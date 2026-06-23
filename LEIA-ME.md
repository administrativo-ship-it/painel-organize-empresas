# Portais de Mentoria — Organize Empresas

Sistema de **fanpages individuais por cliente**, com a identidade visual da Organize Empresas.
Cada cliente acessa em um só lugar: **agenda da mentoria, mentorias gravadas, Trello e Drive**.

## Arquivos

| Arquivo | O que é |
|---|---|
| `cliente.html` | A página (modelo único) que o cliente acessa. Abre via `cliente.html?id=NOME`. |
| `gerar.html` | Painel da assessora para criar novos portais (formulário → link pronto). |
| `clientes/*.json` | Os dados de cada cliente (criados automaticamente pelo gerador). |
| `clientes/exemplo.json` | Cliente de exemplo para teste. |

---

## Como publicar (uma vez só)

Os dois arquivos `.html` precisam ficar no repositório do GitHub Pages que a Organize já usa:
**`administrativo-ship-it/painel-organize-empresas`**.

1. Subir `cliente.html`, `gerar.html` e a pasta `clientes/` para a **raiz do repositório**.
2. GitHub Pages já está ligado → os links ficam assim:
   - Gerador (assessora): `https://administrativo-ship-it.github.io/painel-organize-empresas/gerar.html`
   - Portal do cliente: `https://administrativo-ship-it.github.io/painel-organize-empresas/cliente.html?id=...`

> Posso fazer esse upload automaticamente pelo token do GitHub, se você autorizar.

---

## Como a assessora cria um portal novo

1. Abre o **`gerar.html`**.
2. Na 1ª vez, clica em **"Conectar GitHub"** e cola o token (fica salvo só no navegador dela).
3. Preenche: nome da empresa, serviço, responsável, **logo**, e os links de **Agenda, Trello e Drive**.
4. Clica em **"Criar portal do cliente"** → recebe o link pronto.
5. Usa o botão **"Enviar no WhatsApp"** ou **"Copiar link"** para mandar ao cliente.

Pronto — o portal está no ar e se atualiza sozinho.

---

## Onde pegar cada link

- **Google Agenda (ID):** Google Agenda → a agenda do cliente → ⚙️ Configurações → **"Integrar agenda"** → copiar o **ID da agenda** (ex.: `algo@group.calendar.google.com`). Cole no campo do gerador. Também aceita o link de incorporação (extrai o ID sozinho).
- **Trello:** abrir o quadro → copiar o link da barra de endereço.
- **Drive:** abrir a pasta da empresa → botão direito → "Compartilhar" / copiar link.

---

## Agenda privada + login (privacidade)

Por decisão do projeto, a agenda é **privada**: o cliente entra com a conta Google dele para ver
a agenda e as gravações. Para funcionar:

- A agenda da mentoria precisa estar **compartilhada** (permissão de leitura) com o **e-mail Google do cliente**.
- O domínio `administrativo-ship-it.github.io` já está autorizado no cliente OAuth do Google
  (`organize-painel`). Se mudar o endereço de hospedagem, atualizar as "Origens JavaScript autorizadas".

---

## Mentorias gravadas (automático)

A página lê os eventos do Google Agenda e detecta gravações automaticamente quando o **link da
gravação está na descrição do evento** (Drive, Meet, YouTube, Loom etc.) ou anexado ao evento.

➡️ **Padrão recomendado:** depois de cada mentoria, colar o link da gravação na **descrição do
evento** correspondente no Google Agenda. A aba "Mentorias Gravadas" se atualiza sozinha.

---

## Identidade visual

- Azul-marinho `#000080` · Ciano `#8BD4E0` · Dourado `#F4A800` · Fonte **DM Sans**
- Logo oficial Organize Empresas embutida (SVG).
