#!/usr/bin/env python3
"""
Sincroniza os prazos dos cards do Pipefy com uma agenda compartilhada do Google Calendar.

O que ele faz a cada execucao:
  - Pega um token novo no Pipefy (conta de servico), entao nao depende de token salvo.
  - Le todos os cards do pipe que tem data de entrega.
  - Cria ou atualiza um evento de dia inteiro na agenda para cada card.
  - Mudou a data no Pipefy? O evento se move sozinho na proxima execucao.
  - Card perdeu a data ou foi arquivado? O evento correspondente e removido.
  - Nao cria duplicata: o ID do evento e derivado do ID do card.
  - So mexe nos eventos que ele mesmo criou. Reunioes lancadas na mao ficam intactas.
  - Em regime normal (nada mudou) ele nao escreve nada, so confere.

Uso:
    python sync.py              # roda um ciclo de sincronizacao
    python sync.py --discover   # lista os campos do seu pipe (para conferir os nomes)
"""

import os
import sys
import json
import hashlib
import logging
from datetime import date, timedelta

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ----------------------------------------------------------------------------
# CONFIGURACAO (vem do arquivo .env)
# ----------------------------------------------------------------------------

PIPE_ID = os.environ.get("PIPEFY_PIPE_ID", "302422931")
PIPEFY_CLIENT_ID = os.environ.get("PIPEFY_CLIENT_ID")
PIPEFY_CLIENT_SECRET = os.environ.get("PIPEFY_CLIENT_SECRET")
PIPEFY_TOKEN_URL = os.environ.get(
    "PIPEFY_TOKEN_URL",
    "https://signin.pipefy.com/realms/pipefy/protocol/openid-connect/token",
)
PIPEFY_GRAPHQL_URL = "https://api.pipefy.com/graphql"

GOOGLE_SA_JSON = os.environ.get("GOOGLE_SA_JSON", "service-account.json")
CALENDAR_ID = os.environ.get("CALENDAR_ID")

# O script tenta achar sozinho o campo do cliente e do responsavel pelo nome.
# Se errar, ajuste estes trechos no .env para bater com o rotulo do seu pipe.
CLIENT_FIELD_HINT = os.environ.get("CLIENT_FIELD_HINT", "cliente").lower()
RESP_FIELD_HINT = os.environ.get("RESP_FIELD_HINT", "respons").lower()

# Marca que identifica os eventos criados por este script.
SOURCE_TAG = "pipefy-sync"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("sync")


def require(nome, valor):
    if not valor:
        log.error(f"Falta configurar {nome} no .env")
        sys.exit(1)


# ----------------------------------------------------------------------------
# PIPEFY
# ----------------------------------------------------------------------------

def get_pipefy_token():
    resp = requests.post(
        PIPEFY_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": PIPEFY_CLIENT_ID,
            "client_secret": PIPEFY_CLIENT_SECRET,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        log.error(f"Nao consegui token no Pipefy ({resp.status_code}): {resp.text[:300]}")
        resp.raise_for_status()
    return resp.json()["access_token"]


CARDS_QUERY = """
query ($pipeId: ID!, $cursor: String) {
  cards(pipe_id: $pipeId, first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        title
        due_date
        current_phase { name }
        assignees { name }
        fields { name value }
      }
    }
  }
}
"""


def fetch_cards(token):
    headers = {"Authorization": f"Bearer {token}"}
    cursor = None
    cards = []
    while True:
        resp = requests.post(
            PIPEFY_GRAPHQL_URL,
            json={"query": CARDS_QUERY, "variables": {"pipeId": PIPE_ID, "cursor": cursor}},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"Pipefy GraphQL retornou erro: {data['errors']}")
        conn = data["data"]["cards"]
        for edge in conn["edges"]:
            cards.append(edge["node"])
        if conn["pageInfo"]["hasNextPage"]:
            cursor = conn["pageInfo"]["endCursor"]
        else:
            break
    return cards


def field_map(card):
    return {
        (f.get("name") or "").strip().lower(): (f.get("value") or "")
        for f in (card.get("fields") or [])
    }


def extract_cliente(card):
    for name, value in field_map(card).items():
        if CLIENT_FIELD_HINT in name and value:
            return value
    return ""


def extract_responsavel(card):
    nomes = [a["name"] for a in (card.get("assignees") or []) if a.get("name")]
    if nomes:
        return ", ".join(nomes)
    for name, value in field_map(card).items():
        if RESP_FIELD_HINT in name and value:
            return value
    return ""


def due_as_date(card):
    due = card.get("due_date")
    if not due:
        return None
    try:
        return date.fromisoformat(due[:10])
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# GOOGLE CALENDAR
# ----------------------------------------------------------------------------

def calendar_service():
    creds = service_account.Credentials.from_service_account_file(
        GOOGLE_SA_JSON, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def event_id_for(card_id):
    # ID estavel por card: mesmo card sempre cai no mesmo evento (sem duplicata).
    return "pf" + hashlib.sha1(str(card_id).encode()).hexdigest()


def event_signature(summary, start_date, description):
    basis = json.dumps(
        {"s": summary, "d": start_date, "desc": description},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def build_event(card):
    d = due_as_date(card)
    cliente = extract_cliente(card)
    resp = extract_responsavel(card)
    fase = (card.get("current_phase") or {}).get("name", "")
    title = card.get("title") or "Sem titulo"

    summary = title

    description = "\n".join([
        f"Demanda: {title}",
        f"Responsavel: {resp or '-'}",
        f"Etapa: {fase or '-'}",
        "",
        f"Card no Pipefy: https://app.pipefy.com/open-cards/{card['id']}",
        "",
        "Evento gerado automaticamente a partir do Pipefy.",
        "Para mudar a data, altere no card. Nao edite por aqui.",
    ])

    start_date = d.isoformat()
    sig = event_signature(summary, start_date, description)

    return {
        "id": event_id_for(card["id"]),
        "summary": summary,
        "description": description,
        "start": {"date": start_date},
        "end": {"date": (d + timedelta(days=1)).isoformat()},
        "transparency": "transparent",
        "reminders": {"useDefault": True},
        "extendedProperties": {
            "private": {"source": SOURCE_TAG, "cardId": str(card["id"]), "sig": sig}
        },
    }


def list_managed_events(service):
    # cardId -> (eventId, assinatura) de tudo que este script ja criou na agenda.
    result = {}
    page_token = None
    while True:
        resp = service.events().list(
            calendarId=CALENDAR_ID,
            privateExtendedProperty=f"source={SOURCE_TAG}",
            showDeleted=False,
            singleEvents=True,
            maxResults=250,
            pageToken=page_token,
        ).execute()
        for ev in resp.get("items", []):
            priv = ev.get("extendedProperties", {}).get("private", {})
            cid = priv.get("cardId")
            if cid:
                result[cid] = (ev["id"], priv.get("sig"))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return result


def insert_event(service, event):
    try:
        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    except HttpError as e:
        if e.resp.status == 409:  # ja existe, atualiza
            service.events().update(
                calendarId=CALENDAR_ID, eventId=event["id"], body=event
            ).execute()
        else:
            raise


def update_event(service, event):
    service.events().update(
        calendarId=CALENDAR_ID, eventId=event["id"], body=event
    ).execute()


# ----------------------------------------------------------------------------
# CICLO PRINCIPAL
# ----------------------------------------------------------------------------

def sync():
    require("PIPEFY_CLIENT_ID", PIPEFY_CLIENT_ID)
    require("PIPEFY_CLIENT_SECRET", PIPEFY_CLIENT_SECRET)
    require("CALENDAR_ID", CALENDAR_ID)

    token = get_pipefy_token()
    cards = fetch_cards(token)
    service = calendar_service()
    managed = list_managed_events(service)

    current_ids = set()
    criados = atualizados = inalterados = 0

    for card in cards:
        if due_as_date(card) is None:
            continue
        cid = str(card["id"])
        current_ids.add(cid)
        event = build_event(card)
        sig = event["extendedProperties"]["private"]["sig"]
        existing = managed.get(cid)

        if existing is None:
            insert_event(service, event)
            criados += 1
        elif existing[1] != sig:
            update_event(service, event)
            atualizados += 1
        else:
            inalterados += 1

    removidos = 0
    for cid, (eid, _sig) in managed.items():
        if cid not in current_ids:
            try:
                service.events().delete(calendarId=CALENDAR_ID, eventId=eid).execute()
                removidos += 1
            except HttpError as e:
                if e.resp.status not in (404, 410):
                    raise

    log.info(
        f"ok | prazos: {len(current_ids)} | criados: {criados} | "
        f"atualizados: {atualizados} | removidos: {removidos} | sem mudanca: {inalterados}"
    )


def discover():
    require("PIPEFY_CLIENT_ID", PIPEFY_CLIENT_ID)
    require("PIPEFY_CLIENT_SECRET", PIPEFY_CLIENT_SECRET)
    token = get_pipefy_token()
    cards = fetch_cards(token)
    nomes = set()
    com_data = 0
    for card in cards:
        if due_as_date(card) is not None:
            com_data += 1
        for f in (card.get("fields") or []):
            if f.get("name"):
                nomes.add(f["name"])
    print(f"\nCards no pipe: {len(cards)} | com data de entrega: {com_data}\n")
    print("Campos encontrados nos cards:")
    for n in sorted(nomes):
        print(f"  - {n}")
    print(
        "\nConfira se existe um campo de cliente e um de responsavel acima."
        "\nSe o nome for diferente do padrao, ajuste CLIENT_FIELD_HINT / "
        "RESP_FIELD_HINT no .env (basta um pedaco do nome, em minusculo)."
    )


if __name__ == "__main__":
    if "--discover" in sys.argv:
        discover()
    else:
        sync()
