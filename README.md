# Diário de Bordo (Coolify)

Digitaliza e automatiza o diário de bordo dos motoristas (solução «Logbook» da FMS), construído sobre a mesma base da app Auditoria Via Verde: FastAPI + SQLite, empresas, gestores/utilizadores, superadministrador, GPSWox e Trust Track.

## Como funciona
- **Estados do motorista**: na app (link pessoal, no telemóvel) escolhe *Trabalho* ou *Descanso*. A **condução** é detetada automaticamente pelo objeto GPS atribuído.
- **Geração**: o dia divide-se em períodos de 5 min. Algum ponto com velocidade > 1 km/h → condução; senão, «Trabalho» indicado na app → trabalho; senão → descanso.
- **Relatórios** (por motorista e dia): *Sem relatório* → *Em preenchimento* → *Assinado*. Assinar tem 3 passos: revisão do dia (corrigir trabalho/descanso, nunca a condução), dados da viatura e viagens (preenchidos pelo GPS) e assinatura. Cada passo pode ser guardado para depois. Só se assinam dias terminados.
- **Backoffice → Relatórios**: filtro por motoristas e período, PDF individual e **descarga em lote (ZIP)**. API: `GET /api/logbooks?from=&to=&driver=` com `Authorization: Bearer <token da empresa>`.
- O relatório assinado fica congelado (instantâneo + PDF + SHA-256 + IP).

## Verificação noturna e alertas
Todas as noites (às `NIGHTLY_HOUR`, por omissão 3h) a app calcula os últimos `NIGHTLY_DAYS` dias de cada motorista ativo e gera alertas no Painel: dia com condução por assinar (há 2+ dias), condução acima de 9 h, sem dados GPS, objeto sem posição há 48 h, «Trabalho» ligado há 14 h e motorista sem objeto. O gestor pode **Verificar agora** ou **Ignorar** um alerta.

## Quilómetros
A distância por viagem e por dia é medida pelo GPS. O conta-quilómetros: o inicial vem do último relatório assinado do motorista e o final é sugerido (inicial + distância GPS); o motorista confirma com o painel da viatura.

## Deploy no Coolify
1. Repositório Git com esta pasta → recurso *Docker Compose* (ou Dockerfile, porta 8000).
2. Variáveis: copie `.env.example` (obrigatório: `APP_PASSWORD`, `GPSWOX_URL` se usar Track).
3. Volume persistente em `/data` (base de dados e PDFs assinados).
4. Entre como `admin`, crie a empresa, ligue o GPS em *Empresas*, sincronize em *Objetos GPS*, crie motoristas, atribua o objeto e envie o link (WhatsApp/SMS).

## Testes
`pip install -r requirements.txt pillow pytest && pytest tests`
