# A Ponte: Agente A2A com MCP por Dentro

Repositório com a implementação completa do subsistema de agendamento e reserva de salas da Hill Valley Tech, operando com um servidor MCP em transporte Streamable HTTP (com ciclo MRTR para resolução de conflitos de disponibilidade) e um Agente A2A v1.0 exposto sobre JSON-RPC 2.0.

---

## Como Rodar

A execução é puramente em Python 3.10+ nativo, sem bibliotecas externas.

### 1. Definir Segredo de Integridade
Gere uma chave criptográfica segura para a assinatura do `requestState` e exporte na variável de ambiente:
```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")