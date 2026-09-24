# 1. Grava a versao corrigida do agente
import os
import sys
import json
import uuid
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import urllib.request

PORT = int(os.environ.get("A2A_PORT", 7300))
MCP_URL = os.environ.get("MCP_URL", "http://localhost:7301/mcp")

TASKS = {}
DISCOVERED_TOOLS = []
POLITICA_VERSAO = "2026-11-01"

def invoke_mcp(method, params, traceparent=None):
    req_id = str(uuid.uuid4())
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}}
    }
    if traceparent:
        meta["traceparent"] = traceparent
    params["_meta"] = meta

    headers = {
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method
    }
    if method == "tools/call":
        headers["Mcp-Name"] = params.get("name", "")
    elif method == "resources/read":
        headers["Mcp-Name"] = params.get("uri", "")

    req_data = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}).encode("utf-8")
    req = urllib.request.Request(MCP_URL, data=req_data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8"))

def setup_agent():
    global DISCOVERED_TOOLS, POLITICA_VERSAO
    try:
        list_res = invoke_mcp("tools/list", {})
        DISCOVERED_TOOLS = list_res.get("result", {}).get("tools", [])
        res_politica = invoke_mcp("resources/read", {"uri": "politica://uso"})
        contents = res_politica.get("result", {}).get("contents", [{}])[0].get("text", "")
        if "versao:" in contents:
            POLITICA_VERSAO = contents.splitlines()[0].split("versao:")[1].strip()
    except Exception as e:
        sys.stderr.write(f"Aviso setup_agent: {e}\n")

def extrair_texto_msg(msg_obj):
    if not isinstance(msg_obj, dict):
        return ""
    if "parts" in msg_obj and isinstance(msg_obj["parts"], list) and len(msg_obj["parts"]) > 0:
        part = msg_obj["parts"][0]
        if isinstance(part, dict):
            return part.get("text", "").strip()
        elif isinstance(part, str):
            return part.strip()
    if "text" in msg_obj:
        return str(msg_obj["text"]).strip()
    return ""

def montar_status(state, texto_msg=""):
    st = {"state": state}
    if texto_msg:
        st["message"] = {
            "role": "ROLE_AGENT",
            "parts": [{"text": texto_msg}]
        }
    return st

class A2AServerHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_json(self, status_code, body_dict):
        payload = json.dumps(body_dict).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/.well-known/agent-card.json":
            card = {
                "id": "agent-reservas-hill-valley",
                "name": "Agente de Reservas de Salas",
                "description": "Agente A2A para gerenciamento e reserva de salas",
                "version": "1.0",
                "protocolVersion": "1.0",
                "supportedInterfaces": [
                    {
                        "protocolBinding": "JSONRPC",
                        "url": "http://localhost:7300/a2a",
                        "protocolVersion": "1.0"
                    }
                ],
                "capabilities": {
                    "tasks": True
                },
                "skills": [
                    {
                        "id": "reservar-sala",
                        "name": "Reservar Sala",
                        "description": "Capacidade para reservar salas de reunião"
                    }
                ]
            }
            self.send_json(200, card)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if urlparse(self.path).path != "/a2a":
            self.send_response(404)
            self.end_headers()
            return

        traceparent = self.headers.get("traceparent")
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        try:
            data = json.loads(body)
        except Exception:
            self.send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            return

        req_id = data.get("id")
        method = data.get("method")
        params = data.get("params", {})

        if method == "GetTask":
            t_id = params.get("taskId") or params.get("id")
            t_entry = TASKS.get(t_id)
            if not t_entry:
                self.send_json(400, {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Task not found"}})
                return
            self.send_json(200, {"jsonrpc": "2.0", "id": req_id, "result": {"task": t_entry["data"]}})
            return

        elif method == "SendMessage":
            msg_obj = params.get("message", {})
            task_id = params.get("taskId") or msg_obj.get("taskId")
            texto = extrair_texto_msg(msg_obj)

            # 1. Continuacao de Task existente
            if task_id:
                t_entry = TASKS.get(task_id)
                if not t_entry:
                    self.send_json(400, {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Task inexistente"}})
                    return

                t_data = t_entry["data"]
                curr_state = t_data.get("status", {}).get("state", "")
                if curr_state in ["TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED"]:
                    self.send_json(400, {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32602, "message": "Task em estado terminal"}
                    })
                    return

                m = re.match(r"^escolha=(.+)$", texto)
                if not m:
                    self.send_json(400, {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Formato de resposta invalido"}})
                    return

                escolha = m.group(1).strip()
                alts = t_entry["alternativas"]

                if escolha == "recusar":
                    call_params = {
                        "name": "reservar_sala",
                        "arguments": t_entry["orig_args"],
                        "requestState": t_entry["requestState"],
                        "inputResponses": {
                            t_entry["req_key"]: {"action": "decline"}
                        }
                    }
                    invoke_mcp("tools/call", call_params, traceparent)
                    t_data["status"] = montar_status("TASK_STATE_CANCELED", "Reserva cancelada")
                    self.send_json(200, {"jsonrpc": "2.0", "id": req_id, "result": {"task": t_data}})
                    return

                if escolha not in alts:
                    linha_alts = "alternativas: " + ", ".join(alts)
                    t_data["status"] = montar_status("TASK_STATE_INPUT_REQUIRED", linha_alts)
                    self.send_json(200, {"jsonrpc": "2.0", "id": req_id, "result": {"task": t_data}})
                    return

                # Escolha aceita
                call_params = {
                    "name": "reservar_sala",
                    "arguments": t_entry["orig_args"],
                    "requestState": t_entry["requestState"],
                    "inputResponses": {
                        t_entry["req_key"]: {"action": "accept", "content": {"sala": escolha}}
                    }
                }
                mcp_resp = invoke_mcp("tools/call", call_params, traceparent)
                struct = mcp_resp.get("result", {}).get("structuredContent", {})
                t_data["status"] = montar_status("TASK_STATE_COMPLETED", f"Reserva concluida: {struct.get('reserva')}")
                t_data["artifacts"] = [{
                    "name": "reserva",
                    "parts": [{"text": json.dumps(struct)}]
                }]
                self.send_json(200, {"jsonrpc": "2.0", "id": req_id, "result": {"task": t_data}})
                return

            # 2. Abertura de Nova Task
            new_task_id = f"task-{uuid.uuid4().hex[:8]}"
            ctx_id = f"ctx-{uuid.uuid4().hex[:8]}"
            task_obj = {
                "id": new_task_id,
                "contextId": ctx_id,
                "status": montar_status("TASK_STATE_WORKING"),
                "artifacts": []
            }
            TASKS[new_task_id] = {
                "data": task_obj,
                "requestState": None,
                "alternativas": [],
                "orig_args": {},
                "req_key": None
            }

            match = re.search(r"reservar\s+sala=(?P<sala>\S+)\s+inicio=(?P<inicio>\S+)\s+fim=(?P<fim>\S+)\s+responsavel=(?P<responsavel>.+)$", texto)
            if not match:
                task_obj["status"] = montar_status("TASK_STATE_FAILED", "Formato de comando invalido")
                self.send_json(200, {"jsonrpc": "2.0", "id": req_id, "result": {"task": task_obj}})
                return

            args = match.groupdict()
            args["responsavel"] = args["responsavel"].strip()

            call_resp = invoke_mcp("tools/call", {"name": "reservar_sala", "arguments": args}, traceparent)
            res = call_resp.get("result", {})

            if res.get("isError"):
                err_msg = res.get("content", [{}])[0].get("text", "Erro de execucao")
                task_obj["status"] = montar_status("TASK_STATE_FAILED", err_msg)
                self.send_json(200, {"jsonrpc": "2.0", "id": req_id, "result": {"task": task_obj}})
                return

            if res.get("resultType") == "complete":
                struct = res.get("structuredContent", {})
                task_obj["status"] = montar_status("TASK_STATE_COMPLETED", f"Reserva concluida: {struct.get('reserva')}")
                task_obj["artifacts"] = [{
                    "name": "reserva",
                    "parts": [{"text": json.dumps(struct)}]
                }]
                self.send_json(200, {"jsonrpc": "2.0", "id": req_id, "result": {"task": task_obj}})
                return

            if res.get("resultType") == "input_required":
                req_state = res.get("requestState")
                req_map = res.get("inputRequests", {})
                req_key = next(iter(req_map.keys()))
                elicitation = req_map[req_key]["params"]
                enum_vals = elicitation["requestedSchema"]["properties"]["sala"].get("enum")
                if not enum_vals:
                    enum_vals = [elicitation["requestedSchema"]["properties"]["sala"]["const"]]

                TASKS[new_task_id]["requestState"] = req_state
                TASKS[new_task_id]["alternativas"] = enum_vals
                TASKS[new_task_id]["orig_args"] = args
                TASKS[new_task_id]["req_key"] = req_key

                linha_alts = "alternativas: " + ", ".join(enum_vals)
                task_obj["status"] = montar_status("TASK_STATE_INPUT_REQUIRED", linha_alts)
                self.send_json(200, {"jsonrpc": "2.0", "id": req_id, "result": {"task": task_obj}})
                return

        else:
            self.send_json(400, {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}})

def run():
    setup_agent()
    server = HTTPServer(("0.0.0.0", PORT), A2AServerHandler)
    sys.stderr.write(f"Servidor A2A iniciado na porta {PORT}\n")
    server.serve_forever()

if __name__ == "__main__":
    run()