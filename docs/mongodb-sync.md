# MongoDB Atlas analytics: operação e desativação de legado

## Destino único de analytics

O único destino de escrita do sincronizador é **MongoDB Atlas**. O PostgreSQL
`segundo_prod` do Neon continua intacto, acessado exclusivamente com role
read-only para extração de dados. O banco PostgreSQL Analytics do homelab
não é mais gerenciado por este repositório.

### Coleções atualmente preservadas

O ambiente mantém o database `ouros_analytics_poc` (ou nome iniciado com esse
prefixo), assim como `poc_analytics_rows`, `poc_farm_monthly`,
`poc_metric_buckets` e `poc_sync_state`. Os nomes históricos foram
preservados para não perder os documentos validados na prova de conceito.

### Configuração necessária

No GitHub Actions:
- Secrets: `PRODUCTION_DATABASE_URL` e `MONGODB_URI`.
- Variables: `MONGO_SYNC_ENABLED=true` e opcionalmente `MONGODB_DATABASE`.
- Agenda: `17,47 * * * *` (UTC). Disparo manual: `workflow_dispatch`.
- O evento `push` não sincroniza dados. A CI não exige acesso ao banco de produção.

A conexão com PostgreSQL usa `sslmode=verify-full`, com verificação de
certificado/hostname e CA de confiança. A URI MongoDB não pode conter opções
`tlsInsecure=true`, `tlsAllowInvalidCertificates=true` ou
`tlsAllowInvalidHostnames=true`.

A função `validate_source_database` no próprio `scripts/sync_mongodb.py`
confirma `current_database()=segundo_prod` e a existência de
`public.enterprises` e `public.farms` antes de qualquer escrita no Atlas,
inclusive na execução local (`python -m scripts.sync_mongodb`). O GitHub
Actions reutiliza a mesma função no preflight. Origem incorreta interrompe
a sincronização sem alterar coleções nem o checkpoint.

## Consistência e custo

- O algoritmo atual realiza reconciliação **completa** de cada dataset.
- Documentos usam IDs estáveis, permitindo UPSERTs e remoções por snapshot.
- A leitura da origem ocorre em transação `REPEATABLE READ` somente leitura.
- A escrita em múltiplas coleções no Atlas **não é atomicamente publicada**.
- `poc_sync_state.status` informa o último estado da sincronização; consumidores
  devem considerar status e recência antes de servir os gráficos.
- Uma execução pode falhar após escrever algumas coleções. No erro, o watermark
  bem-sucedido anterior não é avançado; uma nova execução tenta reconciliar tudo.
- O intervalo de ~30 minutos não é garantia de atualização em tempo real.

Para validar uma mudança com auditoria de paridade, execute o script
`scripts/audit_mongodb_poc.py` após obter um snapshot válido e leia o
`POC AUDIT PASSED`. A auditoria usa `~/.ouros-analytics-poc-baseline.json`
com criação exclusiva, permissões `0600` e proteção contra symlinks. Ela é
uma ferramenta de diagnóstico; o job periódico executa somente o sync.

## O que a remoção do GitHub NÃO faz

**Nenhum comando remoto de desativação é realizado pelo merge desta PR.**
Em particular, não executa:
- `DROP DATABASE` ou `DROP SCHEMA` no PostgreSQL do homelab.
- Remoção de container, Docker volume ou dados persistentes.
- `systemctl stop/disable` de timer ou service existentes.
- Alterações em Neon, Tailscale, Prometheus, Infisical ou Discloud.
- Migração automática do `ms-telemetry-dashboard-service` para Atlas.

O antigo `ouros-analytics-sync.timer`, se ainda estiver habilitado no
homelab, pode **continuar executando a implementação antiga** até ser
desabilitado deliberadamente. Remover código do GitHub não desativa um serviço
systemd já instalado na máquina.

## Checklist para corte definitivo do legado

1. Confirmar alguns ciclos agendados do `atlas-sync` na branch `main` com
   `success`, watermark recente e quantidade de documentos consistente.
2. Migrar os consumidores das views `analytics.dashboard_*` para consultas
   MongoDB Atlas no repositório do telemetry. A indisponibilidade temporária
   já foi comunicada, mas confirme os efeitos nos endpoints.
3. Garantir que nenhum outro consumidor precise de `analytics_ro`,
   `analytics_sync_rw`, `ANALYTICS_SYNC_DATABASE_URL` ou do Postgres Analytics.
4. Fazer backup/snapshot com retenção e documentar o procedimento de rollback.
5. Em janela operacional separada, desabilitar timer e serviço antigos no
   homelab e observar o comportamento do telemetry. **Não é parte da PR.**
6. Remover containers/volumes PostgreSQL Analytics e credenciais legadas
   somente após validação. Nunca remover PostgreSQL de produção.

Até concluir essa lista, o legado no homelab existe como infraestrutura
não gerenciada por este repositório. Não confundir essa situação com Mongo-only
na infraestrutura física.
