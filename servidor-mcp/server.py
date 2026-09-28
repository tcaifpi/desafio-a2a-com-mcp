import os
import sys
import json
import hmac
import hashlib
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

PORT = int(os.environ.get("MCP_PORT", 7301))
# Validacao obrigatoria de integridade do requestState
SECRET_HEX = os.environ.get("REQUEST_STATE_SECRET", "").strip()
if not SECRET_HEX:
    sys.stderr.write("ERRO FATAL: Variavel de ambiente REQUEST_STATE_SECRET obrigatoria nao configurada.\n")
    sys.exit(1)

try:
    SECRET_BYTES = bytes.fromhex(SECRET_HEX)
    if len(SECRET_BYTES) < 32:
        raise ValueError("Chave deve ter pelo menos 32 bytes")
except Exception as e:
    sys.stderr.write(f"ERRO FATAL: REQUEST_STATE_SECRET invalida: {e}\n")
    sys.exit(1)
SECRET = bytes.fromhex(SECRET_HEX) if SECRET_HEX else b"00"*32

BASE_DIR = os.path.dirname(__file__)
SALAS_FILE = os.path.abspath(os.path.join(BASE_DIR, "..", "dados", "salas.json"))
RESERVAS_FILE = os.path.abspath(os.path.join(BASE_DIR, "..", "dados", "reservas.json"))
POLITICA_FILE = os.path.abspath(os.path.join(BASE_DIR, "..", "dados", "politica-de-uso.md"))

with open(SALAS_FILE, "r", encoding="utf-8") as f:
    SALAS = json.load(f)

# Cópia para mutação em memória durante os testes
RESERVAS = []
def reset_reservas():
    global RESERVAS
    with open(RESERVAS_FILE, "r", encoding="utf-8") as f:
        RESERVAS = json.load(f)

reset_reservas()

with open(POLITICA_FILE, "r", encoding="utf-8") as f:
    POLITICA_RAW = f.read()
    POLITICA_VERSAO = POLITICA_RAW.splitlines()[0].split("versao:")[1].strip()

def sign_state(payload: dict) -> str:
    payload["exp"] = int(time.time()) + 900
    raw_data = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signature = hmac.new(SECRET, raw_data.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{raw_data}.{signature}"

def verify_and_decode_state(token: str):
    if not token or "." not in token:
        return None
    try:
        raw_data, signature = token.rsplit(".", 1)
        expected_sig = hmac.new(SECRET, raw_data.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            return None
        data = json.loads(raw_data)
        if time.time() > data.get("exp", 0):
            return None
        return data
    except Exception:
        return None

def parse_iso(iso_str):
    return datetime.fromisoformat(iso_str)

def validar_regras(sala_id, inicio_str, fim_str):
    if not any(s["id"] == sala_id for s in SALAS):
        return False, f"Sala inexistente: {sala_id}"
    try:
        dt_ini = parse_iso(inicio_str)
        dt_fim = parse_iso(fim_str)
    except Exception:
        return False, "Intervalo invalido: fim deve ser posterior a inicio"

    if dt_fim <= dt_ini:
        return False, "Intervalo invalido: fim deve ser posterior a inicio"

    if (dt_fim - dt_ini).total_seconds() > 7200:
        return False, "Duracao acima do limite: a politica permite no maximo 2 horas"

    if dt_ini.hour < 8 or (dt_fim.hour > 20 or (dt_fim.hour == 20 and (dt_fim.minute > 0 or dt_fim.second > 0))):
        return False, "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"

    return True, None

def conflitos_para_sala(sala_id, inicio_str, fim_str):
    dt_ini = parse_iso(inicio_str)
    dt_fim = parse_iso(fim_str)
    conflitos = []
    for r in RESERVAS:
        if r["sala"] == sala_id:
            r_ini = parse_iso(r["inicio"])
            r_fim = parse_iso(r["fim"])
            if max(dt_ini, r_ini) < min(dt_fim, r_fim):
                conflitos.append(r)
    return conflitos

def buscar_alternativas(sala_id, inicio_str, fim_str):
    orig_sala = next(s for s in SALAS if s["id"] == sala_id)
    candidatas = []
    for s in SALAS:
        if s["id"] == sala_id:
            continue
        if s["capacidade"] >= orig_sala["capacidade"]:
            if len(conflitos_para_sala(s["id"], inicio_str, fim_str)) == 0:
                candidatas.append(s)
    candidatas.sort(key=lambda x: (x["capacidade"], x["id"]))
    return [s["id"] for s in candidatas[:3]]

class MCPServerHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_rpc_response(self, status_code, body_dict):
        payload = json.dumps(body_dict).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if urlparse(self.path).path != "/mcp":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        try:
            data = json.loads(body)
        except Exception:
            self.send_rpc_response(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            return

        req_id = data.get("id")
        method = data.get("method")
        params = data.get("params", {})
        meta = params.get("_meta", {}) if isinstance(params, dict) else {}

        traceparent = meta.get("traceparent", "none")
        sys.stderr.write(f"[MCP-SERVER] method={method} id={req_id} traceparent={traceparent}\n")
        sys.stderr.flush()

        if "io.modelcontextprotocol/protocolVersion" not in meta or "io.modelcontextprotocol/clientCapabilities" not in meta:
            self.send_rpc_response(400, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": "Missing _meta capabilities or protocolVersion"}
            })
            return

        client_caps = meta["io.modelcontextprotocol/clientCapabilities"]

        if method == "tools/list":
            self.send_rpc_response(200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "listar_salas",
                            "description": "Lista todas as salas cadastradas",
                            "inputSchema": {"type": "object", "properties": {}},
                            "outputSchema": {"type": "object", "properties": {"salas": {"type": "array"}}}
                        },
                        {
                            "name": "consultar_disponibilidade",
                            "description": "Consulta se o intervalo para a sala solicitada está livre",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"sala": {"type": "string"}, "inicio": {"type": "string"}, "fim": {"type": "string"}},
                                "required": ["sala", "inicio", "fim"]
                            }
                        },
                        {
                            "name": "reservar_sala",
                            "description": "Cria a reserva de uma sala",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "sala": {"type": "string"},
                                    "inicio": {"type": "string"},
                                    "fim": {"type": "string"},
                                    "responsavel": {"type": "string"}
                                },
                                "required": ["sala", "inicio", "fim", "responsavel"]
                            }
                        }
                    ]
                }
            })
            return

        elif method == "resources/read":
            uri = params.get("uri")
            if uri == "politica://uso":
                self.send_rpc_response(200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "contents": [
                            {"uri": "politica://uso", "mimeType": "text/markdown", "text": POLITICA_RAW}
                        ]
                    }
                })
            else:
                self.send_rpc_response(400, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": f"Resource not found: {uri}"}
                })
            return

        elif method == "tools/call":
            tool_name = params.get("name")
            args = params.get("arguments", {})

            if tool_name == "listar_salas":
                payload_salas = {"salas": SALAS}
                self.send_rpc_response(200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(payload_salas)}],
                        "structuredContent": payload_salas
                    }
                })
                return

            elif tool_name == "consultar_disponibilidade":
                valido, erro = validar_regras(args.get("sala"), args.get("inicio"), args.get("fim"))
                if not valido:
                    self.send_rpc_response(200, {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "isError": True,
                            "content": [{"type": "text", "text": erro}]
                        }
                    })
                    return
                conflitos = conflitos_para_sala(args.get("sala"), args.get("inicio"), args.get("fim"))
                disponivel = len(conflitos) == 0
                res_payload = {"disponivel": disponivel, "conflitos": conflitos}
                self.send_rpc_response(200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(res_payload)}],
                        "structuredContent": res_payload
                    }
                })
                return

            elif tool_name == "reservar_sala":
                if "requestState" in params:
                    decoded_state = verify_and_decode_state(params.get("requestState"))
                    if not decoded_state:
                        self.send_rpc_response(400, {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {"code": -32602, "message": "requestState invalido ou expirado"}
                        })
                        return

                    orig_sala = decoded_state["sala"]
                    orig_ini = decoded_state["inicio"]
                    orig_fim = decoded_state["fim"]
                    orig_resp = decoded_state["responsavel"]
                    chave_requisicao = decoded_state["input_request_key"]

                    responses = params.get("inputResponses", {})
                    user_resp = responses.get(chave_requisicao, {})
                    action = user_resp.get("action")

                    if action in ["decline", "cancel"]:
                        struct_recusa = {"reservado": False, "motivo": "Reserva recusada pelo solicitante"}
                        self.send_rpc_response(200, {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "resultType": "complete",
                                "isError": False,
                                "content": [{"type": "text", "text": json.dumps(struct_recusa)}],
                                "structuredContent": struct_recusa
                            }
                        })
                        return

                    sala_escolhida = user_resp.get("content", {}).get("sala")
                    nova_reserva = {
                        "id": f"res-{len(RESERVAS)+1:04d}",
                        "sala": sala_escolhida,
                        "inicio": orig_ini,
                        "fim": orig_fim,
                        "responsavel": orig_resp
                    }
                    RESERVAS.append(nova_reserva)
                    struct_sucesso = {
                        "reserva": nova_reserva["id"],
                        "reservado": True,
                        "sala": nova_reserva["sala"],
                        "inicio": nova_reserva["inicio"],
                        "fim": nova_reserva["fim"],
                        "responsavel": nova_reserva["responsavel"],
                        "politica": POLITICA_VERSAO
                    }
                    self.send_rpc_response(200, {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "resultType": "complete",
                            "content": [{"type": "text", "text": json.dumps(struct_sucesso)}],
                            "structuredContent": struct_sucesso
                        }
                    })
                    return

                sala = args.get("sala")
                inicio = args.get("inicio")
                fim = args.get("fim")
                responsavel = args.get("responsavel")

                valido, erro = validar_regras(sala, inicio, fim)
                if not valido:
                    self.send_rpc_response(200, {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "isError": True,
                            "content": [{"type": "text", "text": erro}]
                        }
                    })
                    return

                conflitos = conflitos_para_sala(sala, inicio, fim)
                if not conflitos:
                    nova_reserva = {
                        "id": f"res-{len(RESERVAS)+1:04d}",
                        "sala": sala,
                        "inicio": inicio,
                        "fim": fim,
                        "responsavel": responsavel
                    }
                    RESERVAS.append(nova_reserva)
                    struct_sucesso = {
                        "reserva": nova_reserva["id"],
                        "reservado": True,
                        "sala": nova_reserva["sala"],
                        "inicio": nova_reserva["inicio"],
                        "fim": nova_reserva["fim"],
                        "responsavel": nova_reserva["responsavel"],
                        "politica": POLITICA_VERSAO
                    }
                    self.send_rpc_response(200, {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "resultType": "complete",
                            "content": [{"type": "text", "text": json.dumps(struct_sucesso)}],
                            "structuredContent": struct_sucesso
                        }
                    })
                    return

                has_elicitation_form = False
                if isinstance(client_caps, dict):
                    el = client_caps.get("elicitation")
                    if isinstance(el, dict) and "form" in el:
                        has_elicitation_form = True

                if not has_elicitation_form:
                    self.send_rpc_response(400, {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32021,
                            "message": "Client does not support elicitation form mode",
                            "data": {"requiredCapabilities": ["elicitation.form"]}
                        }
                    })
                    return

                alternativas = buscar_alternativas(sala, inicio, fim)
                if not alternativas:
                    self.send_rpc_response(200, {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "isError": True,
                            "content": [{"type": "text", "text": "Sem alternativas disponiveis no intervalo"}]
                        }
                    })
                    return

                req_key = "req_selecao_sala"
                state_token = sign_state({
                    "sala": sala,
                    "inicio": inicio,
                    "fim": fim,
                    "responsavel": responsavel,
                    "input_request_key": req_key,
                    "alternativas": alternativas
                })

                schema_prop = {"type": "string", "enum": alternativas} if len(alternativas) > 1 else {"type": "string", "const": alternativas[0]}

                self.send_rpc_response(200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "resultType": "input_required",
                        "inputRequests": {
                            req_key: {
                                "method": "elicitation/create",
                                "params": {
                                    "mode": "form",
                                    "message": "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
                                    "requestedSchema": {
                                        "type": "object",
                                        "properties": {
                                            "sala": schema_prop
                                        },
                                        "required": ["sala"]
                                    }
                                }
                            }
                        },
                        "requestState": state_token
                    }
                })
                return

            else:
                self.send_rpc_response(400, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": f"Tool not found: {tool_name}"}
                })
                return

def run():
    server = HTTPServer(("0.0.0.0", PORT), MCPServerHandler)
    sys.stderr.write(f"Servidor MCP iniciado na porta {PORT}\n")
    server.serve_forever()

if __name__ == "__main__":
    run()