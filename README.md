# A Ponte: Agente A2A com MCP por Dentro

Repositório com a implementação completa do subsistema de agendamento e reserva de salas da Hill Valley Tech, operando com um servidor MCP em transporte Streamable HTTP (com ciclo MRTR para resolução de conflitos de disponibilidade) e um Agente A2A v1.0 exposto sobre JSON-RPC 2.0.

---

## Como Rodar

A execução é puramente em Python 3.10+ nativo, sem bibliotecas externas.

### 1. Definir Segredo de Integridade
Gere uma chave criptográfica segura para a assinatura do `requestState` e exporte na variável de ambiente (obrigatória para a inicialização do servidor):
```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

### 2. Iniciar o Servidor MCP (Terminal 1)
O servidor valida a presença de `REQUEST_STATE_SECRET` (recusando a subida caso ausente), atende em `http://localhost:7301/mcp` e registra método, id e traceparent em `stderr`:
```bash
python3 servidor-mcp/server.py
```

### 3. Iniciar o Agente A2A (Terminal 2)
O agente publica o card em `/.well-known/agent-card.json` e atende JSON-RPC em `http://localhost:7300/a2``:
```bash
python3 agente/server.py
```

### 4. Executar a Suíte de Testes (Terminal 3)
```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

---

## Onde a Ponte Acontece

A costura de protocolos ocorre no manipulador de requisições em `agente/server.py`.

1. **Interrupção e Pausa da Task:**  
   Quando o cliente A2A envia um `SendMessage` solicitando uma sala ocupada, o agente aciona a tool `reservar_sala` do servidor MCP. O servidor detecta o conflito de horário e retorna `resultType: "input_required"`, acompanhado do enum de salas alternativas e de um `requestState` opaco assinado via HMAC-SHA256. O agente intercepta essa resposta, vincula o `requestState` e os metadados da reserva ao `id` da Task em memória e transita o estado para `TASK_STATE_INPUT_REQUIRED`. A resposta ao cliente A2A devolve estritamente a mensagem `alternativas: <ids>`, preservando a opacidade e sem expor o `requestState`.

2. **Retomada e Desacoplamento:**  
   Ao receber a continuação do cliente contendo `escolha=<id>` (ou `escolha=recusar`), o agente recupera o `requestState` guardado para aquele `taskId`, gera um novo `id` de JSON-RPC e dispara o retry contra a tool `reservar_sala` com o payload de `inputResponses`. O servidor valida a integridade do estado e finaliza a operação, permitindo que o agente transite a Task para `TASK_STATE_COMPLETED` (com o artifact de reserva contendo a versão da política) ou `TASK_STATE_CANCELED@.

---

## Decisões Técnicas

* **Proteção e Integridade do `requestState`:** O payload que trafega no cliente é protegido por assinatura **HMAC-SHA256** utilizando a chave fornecida via `REQUEST_STATE_SECRET` (mínimo de 32 bytes). Caso a variável não esteja definida ou possua tamanho inferior ao exigido, o servidor aborta imediatamente a subida com código de erro fatal, impedindo chaves fracas ou fallback para bytes zerados. Carrega ainda um timestamp de expiração configurado para 15 minutos (`exp`). Qualquer adulteração de conteúdo no cliente ou reenvio expirado resulta na rejeição com código de protocolo `-32602`.
* **Natureza Stateless do Servidor MCP:** O servidor não armazena sessões ou estados pendentes em memória entre a emissão do `input_required` e a chegada do retry. Todos os argumentos originais e a chave de requisição viajam selados no `requestState`. Com isso, a retomada de uma reserva funciona mesmo se o processo do servidor MCP for reiniciado entre os passos.
* **Isolamento de Estado das Tasks:** O agente mantém o mapa de Tasks atrelado ao `taskId`, assegurando que múltiplas requisões pausadas simultaneamente não interfiram entre si nem troquem seus respectivos `requestState`.
* **Propagação de Contexto W3C Traceparent:** O cabeçalho `traceparent` enviado nas mensagens A2A é extraído pelo agente e injetado no bloco `_meta.traceparent` de cada requisição enviada ao servidor MCP, sendo registrado no `stderr` para auditabilidade e rastreabilidade distribuída.

---

## Saída do Validador

```text
trace-id desta execucao: 6efdc7308ee4565f7f47b8b9c8d20005
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e ofrece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escohida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada um com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
