# Diário de Bordo (Coolify)

Digitaliza e automatiza o diário de bordo dos motoristas (solução «Logbook» da FMS), construído sobre a mesma base da app Auditoria Via Verde: FastAPI + SQLite, empresas, gestores/utilizadores, superadministrador, GPSWox e Trust Track.

## Como funciona
- **Estados do motorista**: na app (link pessoal, no telemóvel) escolhe *Trabalho* ou *Descanso*. A **condução** é detetada automaticamente pelo objeto GPS atribuído.
- **Geração**: o dia divide-se em períodos de 5 min. Algum ponto com velocidade > 1 km/h → condução; senão, «Trabalho» indicado na app → trabalho; senão → descanso.
- **Relatórios** (por motorista e dia): *Sem relatório* → *Em preenchimento* → *Assinado*. Assinar tem 3 passos: revisão do dia (corrigir trabalho/descanso, nunca a condução), dados da viatura e viagens (preenchidos pelo GPS) e assinatura. Cada passo pode ser guardado para depois. Só se assinam dias terminados.
- **Backoffice → Relatórios**: filtro por motoristas e período, PDF individual e **descarga em lote (ZIP)**. API: `GET /api/logbooks?from=&to=&driver=` com `Authorization: Bearer <token da empresa>`.
- O relatório assinado fica congelado (instantâneo + PDF + SHA-256 + IP).

## Deploy no Coolify
1. Repositório Git com esta pasta → recurso *Docker Compose* (ou Dockerfile, porta 8000).
2. Variáveis: copie `.env.example` (obrigatório: `APP_PASSWORD`, `GPSWOX_URL` se usar Track).
3. Volume persistente em `/data` (base de dados e PDFs assinados).
4. Entre como `admin`, crie a empresa, ligue o GPS em *Empresas*, sincronize em *Objetos GPS*, crie motoristas, atribua o objeto e envie o link (WhatsApp/SMS).

## Testes
`pip install -r requirements.txt pillow pytest && pytest tests`
