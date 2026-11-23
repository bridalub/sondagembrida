# Prospecção de novas empresas: arquitetura e operação

## Pesquisa planejada (objetivo → plano → seleção → investigação → filas)

1. **O gestor define a pesquisa:**
   - objetivo em linguagem natural;
   - **frentes comerciais**, editáveis no Admin;
   - aplicações de interesse, critérios obrigatórios e preferências (preferências **não** excluem);
   - exclusões explícitas;
   - quantidade por frente e limites.
2. **Plano versionado** (tela *Plano*):
   - pode ser **proposto pela IA** (quando configurada), **montado a partir das frentes** (regras, não IA) ou
     **ajustado pelo gestor**;
   - todo CNAE é conferido na tabela oficial da Receita; código inexistente é rejeitado e listado;
   - exclusões sugeridas pela IA ficam só como sugestão;
   - cada execução registra a versão do plano usada.
3. **Seleção inicial** (base Receita local):
   - **porta de entrada:** CNAE principal **ou** secundário do plano **ou** termo de operação no nome. Uma
     atividade cadastral genérica também entra pelo nome ou por evidência.
   - **pontuação com motivos registrados:** atividade principal +3, secundária +1,5, termo no nome +1,
     matriz +0,5, porte "demais" +0,75, EPP +0,4, e-mail com domínio próprio +0,5, telefone +0,25,
     evidência já existente +1, empresário individual −0,75 (preferência menor, não exclui).
   - **ordem:** por pontuação, **não** por CNPJ.
   - **conhecidas:** contas e leads BRIDA, inclusive bloqueadas, vão para a fila "Conhecida".
   - **progressão:** empresas já consideradas não voltam; a execução seguinte avança para novas.
4. **Investigação:**
   - contatos e site (ver Canais);
   - evidências **com contexto**: menção em contexto de financiamento, seguro, venda, clientes, notícia ou blog
     **não** comprova operação própria (o Sicoob é o caso de regressão);
   - IA opcional.
   - **Aderência:**

     | Aderência | Quando |
     |---|---|
     | SUSTENTADA | O site confirmado ou provável descreve a operação própria, ou a IA citou trecho de operação própria |
     | PROVÁVEL | A atividade principal é da frente |
     | INSUFICIENTE | Entrou por atividade secundária ou nome, sem evidência |
     | FORA | O site mostra que o tema é serviço ou produto, ou de terceiros, **e** a atividade não é da frente; ou exclusão explícita |

     Falta de dado nunca vira FORA.
5. **Filas da lista comercial**, em ordem de precedência:
   1. Conhecida no Salesforce
   2. Não contatar / bloqueada
   3. Retorno agendado
   4. Já abordada
   5. Fora do objetivo
   6. Revisar identidade, situação ou conflito
   7. Investigar operação
   8. Buscar telefone/e-mail
   9. Contato cadastral a confirmar
   10. **Selecionada** (aderência sustentada, ou provável com abordagem exploratória permitida, e com canal
       publicado pela própria empresa)

   As decisões do gestor (incluir, retirar ou corrigir a aderência, com motivo) prevalecem e ficam registradas.

## Descoberta pelo Google (fonte principal) e investigação dirigida pela IA

`PROSPECCAO_BUSCA_PROVEDOR=google` + `SERPAPI_API_KEY` (`prospeccao/services/descoberta_google.py`).

- **Provedor:** resultados do Google pela **SerpApi** (terceiro; não é API oficial do Google). Verificado em
  12/11/2026:
  - a API oficial de busca web do Google (Custom Search JSON API) está fechada a novos clientes e termina em
    01/01/2027;
  - a Places API só permite guardar o `place_id`;
  - o Gemini com Google Search proíbe montar base de dados com os resultados.
- **Prova de origem:** cada consulta grava `search_metadata.id` (identificador da requisição) e a URL do Google
  consultada (sem a chave), em Consultas web → ações.
- **Sem troca silenciosa:** sem chave, a busca fica "aguardando configuração". Fallback só com
  `PROSPECCAO_BUSCA_FALLBACK=openai`, e mostrado como "NÃO é Google".
- **Rodadas:** uma consulta por frente por rodada, primeiro **local** (perfil da empresa: nome, endereço, telefone,
  site) e depois **orgânica** (sites, diretórios, listagens; a IA extrai as empresas do trecho, e o programa confere
  cada valor no texto). Há paginação quando ainda vêm novidades. Quando só vêm conhecidas ou repetidas, a IA propõe
  outra consulta, e o motivo fica registrado.
- **Cruzamento cedo (antes de gastar):**
  - CNPJ citado, ou identificado na base local da Receita por telefone + nome, ou nome + logradouro;
  - Salesforce por CNPJ, telefone, e-mail ou domínio;
  - prospecção anterior: "já pesquisada" reaproveita o registro e não conta.
  - Conhecidas não viram lead nem contam na meta.
- **Sem CNPJ:** a empresa vira lead com o aviso "CNPJ não identificado – identidade a conferir". O escopo vem da
  frente que a encontrou.
- **Investigação dirigida:** a cada passo a IA recebe dados, fontes, cruzamento, lacunas, consultas feitas e
  limites, e escolhe `ABRIR_PAGINA`, `BUSCAR_GOOGLE`, `CONSULTAR_CNPJ`, `ENCERRAR` ou `PENDENCIA`. O programa recusa
  URL, CNPJ ou consulta inventados ou repetidos. Limite por empresa: `max_buscas_web_empresa`; por pesquisa:
  `limite_consultas`.
- **Varredura da base Receita** só acontece se a fonte "Receita" estiver marcada na pesquisa.
- **Verificação:** `python manage.py verificar_integracoes --chamar --teto-usd 0.05 --com-busca` faz 1 consulta
  local real e mostra o identificador da requisição e a URL do Google.

## Rodada autônoma: descoberta e identificação em lotes (18/11/2026)

O botão **Buscar** deve terminar a rodada sem comando manual. Ordem de trabalho:

1. **Descobrir e salvar** todas as candidatas (nada é descartado). Durante a descoberta, cada empresa passa só pelo
   caminho gratuito (site indicado + fontes salvas); **não gasta IA**.
2. **Cruzar cedo** com a carteira; correspondências incertas vão para "a conferir".
3. **Lote de identificação** (`LOTE_IDENTIFICACAO = 5` empresas) a cada rodada de descoberta: fontes salvas e, faltando
   CNPJ, **busca dirigida feita pelo programa** (nome + município + CNPJ; depois telefone e site salvos, até
   `max_buscas_web_empresa`). **Não depende de a IA escolher buscar** nem de haver chamada de IA disponível.
4. Situação cadastral (base local, ou BrasilAPI se o CNPJ não estiver no recorte) e **novo cruzamento pelo CNPJ**.
5. Próximo lote. No fim da descoberta, as restantes passam pela mesma etapa.

- **Contato e identificação são independentes:** telefone encontrado encerra a procura de telefone, nunca a de CNPJ.
- **Limite de páginas** (`site_publico`) suspende só a leitura de sites; a identificação segue pela busca dirigida.
- **Meta = leads completos:** CNPJ ATIVO + cruzado com a carteira (fora dela) + contato. A tela da execução mostra o
  funil: candidatas → com contato → identificadas → ativas e cruzadas → leads completos, e as pendências.
- **Continuação automática** (`pipeline.continuar_automaticamente`): se a execução parou por limite interno (tempo,
  páginas, chamadas de IA, rodadas) com empresa ainda a identificar, salva o progresso e enfileira a próxima execução
  (`retomada_de`), até `MAX_CONTINUACOES = 6`. **Não continua** se: cancelamento/pausa, franquia SerpApi sem saldo,
  teto em dólar atingido, meta cumprida, nenhuma busca dirigida possível restante, ou se a execução **não avançou**
  (ciclo repetido). O motivo aparece em "Continuação automática" na tela.
- **Google sem crédito → OpenAI, automático:** quando a franquia SerpApi acaba (leitura da conta com 0, resposta
  "run out of searches") ou bate o limite de velocidade, o botão Buscar segue sem intervenção pela busca web da OpenAI:
  - **descoberta de empresas novas:** até `PROSPECCAO_DESCOBERTA_RESERVA_MAX` consultas por execução (padrão 6;
    0 desliga), uma por consulta da fila, sem Google Maps nem paginação;
  - **identificação de CNPJ:** 1 busca por empresa, pedindo todas as unidades.
  Ambas usam o mesmo orçamento e param no teto financeiro (da pesquisa e global). Desliga tudo com
  `PROSPECCAO_IDENTIFICACAO_FALLBACK=nenhuma`. Como cada consulta é paga, a execução por franquia esgotada **não**
  enfileira continuação automática: cada clique em Buscar faz uma rodada limitada.

### Vínculo do CNPJ (regras de 18/11/2026, `avaliar_associacao` / `resolver_candidato`)

| Sinais | Decisão |
|---|---|
| Telefone em comum + nome compatível + mesmo município | Vincula (endereço diferente fica registrado como possível mudança) |
| Nome igual à razão social ou ao nome fantasia + mesmo município | Vincula |
| Nome bate, **CNPJ registrado em outra cidade** | Procura filial da mesma raiz no município; sem filial, vincula com o rótulo **"CNPJ ativo localizado em outra cidade"** (o município onde a empresa foi encontrada é mantido). Conta na meta se ATIVO, fora da carteira e com contato |
| Mesmo caso, mas a raiz do CNPJ já é conta do Salesforce | Conhecida (unidade local de conta da carteira) |
| CNPJ já é conta do Salesforce (por qualquer caminho, inclusive o da IA) | Conhecida |
| Nome não bate e CNPJ de outra cidade | Rejeitado (normalmente é o CNPJ do diretório que publicou a empresa) |

- Telefone compara DDD + 8 últimos dígitos (a Receita guarda celular antigo sem o 9).
- Rejeições das regras anteriores são reavaliadas uma vez (marca `[regras-2]`).
- Quando as regras já concluíram e há contato publicado, a IA não é chamada para repetir a avaliação.
- "Meta atingida" só quando os **leads completos** chegam à meta; candidatas suficientes na descoberta
  (`cota_descoberta`) ou fim por tempo não impedem a continuação automática.

## Escopo comercial (pertinência antes do contato)

Definido pelo gestor em 06/11/2026 (`prospeccao/services/escopo.py`). Quatro perfis; **o porte não exclui, só ordena**:

| Perfil | Exemplos (CNAE) |
|---|---|
| Frota própria | transporte de carga e passageiros (49), coleta de resíduos (38), armazenagem e logística (52), entregas (53), locação de veículos |
| Indústria com máquinas | toda a indústria de transformação (10–33), energia, saneamento |
| Agro, construção e máquinas pesadas | agro (01–03), extração (05–09), construção e terraplenagem (41, 42, 431, 439), locação de máquinas, paisagismo |
| Revenda e oficinas | comércio e manutenção de veículos e peças (45), atacado de máquinas e peças, ferragens |

- **Revisar:** possíveis concorrentes ou revendas de combustíveis e lubrificantes (4681, 4731, 4732, 1921, 1922);
  atividade principal fora do escopo com atividade **secundária** dentro (muitas empresas listam atividades que não
  exercem); ou atividade fora com operação própria apontada pela IA ou pela equipe.
- **Fora do escopo:** banco, farmácia, mercado, lanchonete, buffet, escola etc. Nada é apagado: sai da lista padrão
  (filtro "Escopo comercial" mostra tudo) e **não recebe chamada paga**.
- Evidência por palavra-chave **não** traz de volta empresa fora do escopo: ela confunde serviço vendido ("Para o
  Agronegócio" no site de um banco) com operação própria.
- Só empresas dentro dos perfis recebem pesquisa web e IA. As demais passam só pelo caminho gratuito.
- O gestor pode reclassificar no Admin (Oportunidades → escopo); a decisão humana não é recalculada.
- `python manage.py aplicar_escopo [--gravar]` reclassifica as oportunidades existentes.

## Canais de contato

Cada canal guarda, separadamente:
- **Origem:** cadastral, publicação externa ou equipe.
- **Confiança na associação:**

  | Nível | Quando |
  |---|---|
  | Alta | O site publica o mesmo CNPJ |
  | Média | O site é do mesmo grupo ou menciona o nome da empresa; ou o dado é cadastral |
  | Baixa | Associação fraca |
  | Conflito | Possivelmente de terceiro ou de outro estabelecimento |

- **Aplica-se a:** estabelecimento, empresa, matriz, central do grupo ou contador.
- **Nível do canal** (calculado):

  | Nível | Quando |
  |---|---|
  | Cadastral | Receita/Salesforce, não validado (pode ser do contador) |
  | Publicado | lido em site associado à empresa (MÉDIA/ALTA) |
  | Corroborado | o mesmo canal apareceu em **outra** fonte associada (outro domínio, MÉDIA/ALTA). Diretórios não corroboram: costumam copiar a Receita |
  | Pista | diretório, citação da pesquisa web ou site provável (BAIXA/CONFLITO); exige conferência |
  | Confirmado pela equipe / Incorreto | confirmação humana; nunca é sobrescrita por nova coleta |

  Toda nova fonte em que o canal aparece fica em `corroboracoes` (fonte, URL, domínio, confiança, data).
- **Finalidade:** compras, manutenção, comercial, atendimento, financeiro, fiscal, contador ou desconhecida.
- **Departamento:** só quando está rotulado na página.
- **Verificação técnica:** formato, DDD, MX do domínio. **MX válido não comprova que a caixa existe.**
- **Confirmação humana:** atendeu ou incorreto.
- **Restrição e preferência da equipe.**

Regras:
- não se inventa DDD, número, pessoa ou "contato@dominio";
- WhatsApp só conta quando há link publicado;
- telefone ou e-mail usado por 3 ou mais grupos diferentes na base é tratado como compartilhado (provável contador);
- mesmo telefone em vários estabelecimentos do grupo é tratado como central do grupo;
- e-mail de outro domínio citado no site (agência, parceiro) não é associado.

**Procura do site**, nesta ordem:
1. site conhecido;
2. domínio do e-mail (descartado se for de contador ou compartilhado);
3. **busca web** (quando configurada e com orçamento) por CNPJ, razão social e nome fantasia com o município. Vale
   também para empresas com Gmail/Outlook ou sem domínio. Com a OpenAI, é **uma chamada por empresa** com as consultas
   sugeridas.

**Associação site ↔ empresa** (evidências combinadas, não só CNPJ):

| Evidência no site | Resultado |
|---|---|
| mesmo CNPJ publicado | ALTA (estabelecimento) |
| só o CNPJ da matriz, sendo esta empresa filial | MÉDIA, contatos **da matriz** (não confirmam a filial) |
| outro CNPJ da mesma raiz | MÉDIA, contatos **do grupo** (possível vínculo, não confirma a filial) |
| nome distintivo + telefone cadastral ou CEP | MÉDIA |
| nome + logradouro + município | MÉDIA |
| nome, vindo do domínio do e-mail ou do site já conhecido | MÉDIA |
| só o nome | BAIXA: site provável, **para revisão**; contatos não extraídos |
| nome + CNPJ de outro grupo | CONFLITO, para revisão; contatos não extraídos |
| sem o nome | não associado |

**Diretórios e redes sociais** não são lidos pelo sistema. O que a busca diz ter visto neles vira **pista**:
canal com confiança BAIXA e a fonte indicada. A pista não confirma a identidade e só se torna utilizável depois de
confirmada por uma pessoa. Também viram pista: contatos citados pela pesquisa em site que o leitor **não conseguiu
ler** (o motivo da pendência vai na evidência) e contatos citados em página do site associado que o leitor não
encontrou. Telefone e e-mail são buscados juntos, mas um basta para registrar a página.

**Rastreabilidade da pesquisa web:** cada Consulta web guarda a consulta pedida, as **ações executadas** pela ferramenta
(tipo, consulta ou URL aberta), a lista **completa** de fontes (`web_search_call.action.sources`, que inclui mais URLs
que as citações) e as citações, a data da coleta e, para cada resultado, o **resultado da leitura** do site e a
**associação** com a empresa. Uma URL devolvida pela pesquisa não confirma, por si só, que pertence à empresa.

Contatos que a IA extrai do site só são gravados se aparecerem **literalmente** no texto lido (conferência no
programa, além da validação da resposta), e nunca com confiança acima da associação do próprio site.

O sistema **não adivinha domínios** a partir do nome.

**Descoberta por busca web** (etapa 1, por frente): o sistema roda as consultas do plano (as da IA ou os termos da
frente com o município). Um resultado gera seleção quando:
- o CNPJ (publicado no próprio site, citado pela busca ou no trecho do diretório) tem dígito verificador válido e está
  na base Receita do município e da situação do plano. Se o CNPJ veio só citado, o nome também precisa bater;
- ou, sem CNPJ, quando nome **e** telefone citados batem com **um único** estabelecimento do município.

Um CNPJ válido **ausente do recorte mensal** da Receita entra como pendente de consulta complementar (BrasilAPI, se
habilitada). Se não for confirmado, fica em "Revisar identidade". Nada é descartado em silêncio: o que não gera
seleção fica anotado na Consulta web. Páginas com mais de 3 CNPJs são tratadas como listas e ignoradas.

## Limites e orçamento (programa, não IA)

Configurados por pesquisa (Admin → Pesquisas):

| Campo | Padrão |
|---|---|
| `max_buscas_web_empresa` | 3 |
| `max_buscas_descoberta` | 6 por frente |
| `max_paginas_site` | 5 |
| `ia_max_chamadas` | 40 por execução |
| `ia_max_tokens_saida` | 6000 |
| `validade_analise_ia_dias` | 30 |
| `teto_custo_usd` | vazio |

- Com `teto_custo_usd` **vazio, nenhuma chamada paga é feita**. A execução fica PARCIAL, com a pendência registrada.
- O teto é **acumulado por pesquisa**: soma planejamento e todas as execuções.
- `PROSPECCAO_TETO_GLOBAL_USD` (no .env) limita **todas as chamadas pagas desta aplicação** (pesquisas, planos e
  verificações), somadas pelo que está registrado no banco. **Não enxerga** gastos de outros projetos na mesma conta
  OpenAI: para isso, configure também o limite de orçamento do projeto no painel da OpenAI.
- Antes de cada chamada, o custo **máximo** é reservado **no banco** (tabela ReservaCusto, sob trava): entrada estimada
  alta, saída máxima e todas as ações de pesquisa. A soma considera o gasto registrado **e as reservas em andamento de
  outras execuções** (worker, comando, servidor). Se a reserva não couber, nada é enviado.
- Depois da chamada, o custo **medido** (tokens da resposta x preço + ações x US$ 0,01) é gravado e só então a reserva
  é liberada. O conteúdo das pesquisas já está nos tokens de entrada medidos: não há dupla contagem.
- **Não há retentativa automática** (SDK com `max_retries=0`): cada tentativa é uma chamada reservada e registrada.
- Falha **sem medição** (timeout, queda de conexão, 5xx, interrupção): conta o **máximo reservado**, porque pode ter
  sido cobrada. Só erro 4xx de requisição recusada (400/401/403/404/429) conta zero.
- Os registros de custo são gravados fora de transações longas: uma falha posterior não os apaga.
- **Limites técnicos da pesquisa web:** `max_tool_calls` (documentado como "número máximo de chamadas a ferramentas
  embutidas processadas na resposta", aplicado pela API) = ações por chamada; `max_output_tokens`; timeout; uma
  chamada por empresa. O programa confere o número de ações recebidas e alerta se exceder. O conteúdo lido pelas
  pesquisas **não tem limite por parâmetro**: é reservado alto (60 mil tokens por ação) e registrado pelo medido.
- **O que o teto controla (e o que não controla):** o programa só **inicia** uma chamada se a reserva couber no teto.
  A reserva é uma **estimativa conservadora**, não um limite técnico: a tarifa das ações (`max_tool_calls`) e os
  tokens de saída (`max_output_tokens`) são limitados pela API, mas os **tokens de entrada da pesquisa** (conteúdo das
  páginas lidas pela ferramenta) não têm limite por parâmetro. Portanto a chamada **em andamento** pode custar mais que
  a reserva e o total pode ultrapassar o teto nesse valor. O excesso é registrado pelo uso medido e bloqueia as
  chamadas seguintes. O teto não é, portanto, um limite absoluto garantido; para isso, use também o limite de
  orçamento do projeto no painel da OpenAI.
- Ao atingir o teto, as chamadas param e o motivo aparece nos limites da execução.
- Depois de 3 falhas seguidas do provedor de busca, o sistema não insiste.
- Uma análise de IA da mesma empresa, com o mesmo material e ainda dentro da validade, é reaproveitada sem nova chamada.
- Cada consulta web fica em **Consultas web** (Admin), com consulta, resultados, URLs, data, status
  (OK/FALHA/NAO_CONFIGURADA/SEM_ORCAMENTO), custo estimado e duração.
- O programa controla CNPJ, cruzamento, situação cadastral, duplicidade, restrições e histórico. A IA só lê o
  material coletado.
- Citações e contatos que não aparecem literalmente no material são descartados. A IA nunca atribui confiança ALTA a
  um canal.

**Verificação:** `python manage.py verificar_integracoes` mostra quais variáveis estão definidas, sem exibir valores,
e separa "configuração encontrada" de "validada por chamada real". Com `--chamar --teto-usd X [--com-busca]`, faz uma
chamada mínima de IA e uma pesquisa web de 1 ação (reserva de cerca de US$ 0,03), mostra ações, fontes e resultados
aceitos e registra tudo (entra no teto global).

**Piloto controlado, em etapas** (`python manage.py piloto_ia`; cada etapa tem `--teto-usd` próprio):
- sem `--etapa`: simulação gratuita. Sugere uma empresa de **controle** (externa com site e contato já conhecidos) e
  até 4 casos difíceis (e-mail de contador, telefone repetido em outra empresa, filial), com o custo de cada etapa;
- `--etapa plano`: plano por IA, mostrado para revisão antes de qualquer pesquisa;
- `--etapa controle --ids N`: ignora o caminho gratuito e verifica se a pesquisa **reencontra** o site e os contatos
  já conhecidos (ACERTO / PARCIAL / ERRO);
- `--etapa empresas --ids ...`: caminho gratuito primeiro (site conhecido, domínio do e-mail); a pesquisa paga só
  é chamada se ele não associar site com telefone ou e-mail;
- `--etapa descoberta --frente N`: 1 consulta numa frente; as novas empresas ficam aguardando.

O relatório (`--relatorio <execução>`) traz a tabela antes x depois e, com `--planilha arquivo.csv`, a planilha de
revisão dos sites e contatos encontrados. Preenchida com CERTO / ERRADO / NAO_SEI, ela volta com
`--importar-revisao`: CERTO = "conferido pela equipe" (a pista passa a ser utilizável), ERRADO = incorreto. O comando
mostra a precisão. As marcações humanas nunca são sobrescritas por novas coletas.

## Dependências externas desta etapa

| Recurso | Variáveis (.env) | Observação |
|---|---|---|
| IA – OpenAI | `PROSPECCAO_IA_PROVEDOR=openai`, `OPENAI_API_KEY`, `PROSPECCAO_IA_MODELO=gpt-6-luna` | planejamento, interpretação e contatos |
| Pesquisa web – OpenAI `web_search` | `PROSPECCAO_BUSCA_PROVEDOR=openai` (mesma chave) | US$ 10 por 1.000 ações + conteúdo cobrado como entrada |
| Brave Search (opcional) | `BRAVE_SEARCH_API_KEY` | contratação própria; a chave da OpenAI não a habilita |
| Anthropic (opcional) | `PROSPECCAO_IA_PROVEDOR=anthropic` + `ANTHROPIC_API_KEY` | não é exigida |

Preços conferidos em developers.openai.com/api/docs/pricing em 05/11/2026:
- `gpt-6-luna`: US$ 0,10 por MTok de entrada e US$ 0,50 de saída;
- `gpt-5-mini`: US$ 0,25 e US$ 2,00.

Um modelo fora da tabela é estimado pelo preço mais caro, a menos que `PROSPECCAO_IA_PRECO_*` esteja definido.

**Pesquisa web OpenAI × Brave**

| | OpenAI `web_search` | Brave Search API |
|---|---|---|
| Contratação | mesma chave da OpenAI | separada |
| Custo por consulta | US$ 0,01 por ação + conteúdo como entrada (cerca de US$ 0,02 a 0,04 por empresa, com até 3 ações) | US$ 0,005 |
| O que devolve | fontes (URLs) e dados que o modelo diz ter visto, em JSON | URLs, títulos e trechos |
| Leitura de páginas | o modelo lê as páginas, inclusive diretórios; o programa não recebe o texto integral | nenhuma; o sistema lê o site |
| Risco | o modelo pode citar URL não visitada | baixo |
| Controle | URLs fora das fontes da ferramenta são descartadas; contatos só valem se lidos no próprio site ou confirmados | não se aplica |
| Armazenamento | `store=false`; guardamos URLs, dados citados como pista e consumo | URLs |
| Exibição | é preciso mostrar as fontes como links clicáveis | não se aplica |

## Base pública de CNPJ (Receita Federal)

- **Fonte oficial:** `https://arquivos.receitafederal.gov.br`, compartilhamento público, pasta
  `Dados/Cadastros/CNPJ/AAAA-MM/`. A competência mais recente verificada é **2026-10**, com arquivos
  publicados em 13/10/2026.
- **Dimensionamento em 2026-10** (zips compactados):

  | Arquivos | Tamanho | Uso |
  |---|---|---|
  | Estabelecimentos (10) | 5,16 GB | Importados |
  | Empresas (10) | 1,32 GB | Importados |
  | Socios, Simples | ~1 GB | **Não** importados |

  A velocidade medida foi de cerca de 0,84 MB/s, ou seja, cerca de 2 h para baixar tudo.
- **Recorte:** os arquivos são **nacionais**, sem divisão por cidade. Para qualquer recorte é preciso ler os
  10 arquivos de Estabelecimentos inteiros. Eles são lidos **em fluxo, direto de dentro do zip**, sem
  descompactar no disco, e só as linhas da UF e dos municípios do recorte são gravadas.
- **Importação:** `python manage.py importar_receita --competencia 2026-10 --uf SP --municipio GUARULHOS`
  - `--sem-download` usa arquivos oficiais já baixados, sem nenhuma chamada de rede;
  - `--apagar-arquivos` apaga cada zip depois de processado, para liberar disco;
  - `--forcar` recarrega a mesma competência.
  - Rodar de novo **retoma** a carga: o estado é guardado por arquivo e as linhas parciais de um arquivo
    interrompido são refeitas.
  - Só uma carga executa por vez. A carga ativa só é trocada quando a nova termina; enquanto isso, a anterior
    continua valendo.
- **Separada da carteira:** fica nas tabelas `receita_*`. Estar na base **não** significa ser cliente nem
  oportunidade aprovada.
- **Contatos:** telefone e e-mail da base são **cadastrais**. Podem ser do contador e não foram validados.
- **Não importados:** sócios e Simples. A razão social de MEI tem o CPF final removido.
- **Atualização:** a competência é mensal, **não é consulta em tempo real**.
  - A empresa guarda a data de referência e a fonte da situação cadastral.
  - Para liberar uma abordagem, a referência não pode ser mais antiga que
    `ConfiguracaoAnalise.dias_validade_situacao_cadastral` (padrão de 45 dias).
  - Se for, o sistema consulta a BrasilAPI e registra a resposta. Se a consulta falhar, a situação fica
    **pendente**, sem presumir ATIVA.

### Situação cadastral

| Situação | Tratamento |
|---|---|
| ATIVA | Pode ir para a fila (respeitando as demais regras) |
| SUSPENSA / INAPTA | Revisão; não libera |
| BAIXADA / NULA | Fora da fila (status "Fora da fila") |
| Ausente / consulta com falha | Pendente |

O bloqueio interno (Bloqueado no Protheus/Salesforce) é **separado** da situação na Receita: uma conta bloqueada
continua bloqueada mesmo que esteja ATIVA na Receita.

### Pesquisa pela base Receita

- **Recorte:** o filtro de município usa o código da tabela oficial.
- **Atividade:** o filtro de CNAE (principal ou secundário) é opcional. **Sem ele, entram todas as
  atividades.**
- **Situações:** padrão `ATIVA`.
- **Lotes:** o universo é trabalhado em lotes de `limite_resultados` com cursor; cada execução continua de
  onde a anterior parou.
- **Identificação:** o candidato da base já tem CNPJ oficial e é identificado sem chamar a API.
- **Complementação pela internet:** usa o site informado ou o **domínio próprio do e-mail cadastral**.
  - Site que publica o **mesmo CNPJ**: canal "confirmado pela fonte".
  - Site que publica CNPJ de outro estabelecimento do grupo (matriz ou filial): **conflito**, sem associação
    automática.
  - Site sem CNPJ: "encontrado, não confirmado".
  - Consultas ainda válidas não se repetem.

## IA (OpenAI)

- **Etapas:**
  - planejamento (objetivo, frentes, região, produtos, critérios → atividades, termos, CNAEs conferidos na tabela
    oficial, sinais, consultas e justificativa);
  - interpretação do material coletado de **uma** empresa (operação própria × produto vendido × clientes ×
    publicidade; aplicações como **hipótese**; contatos citados literalmente, com departamento e finalidade).
- **Chamada:** Responses API com Structured Outputs (json_schema estrito), `store=false`, `reasoning.effort=low`,
  `max_output_tokens` pelo limite da pesquisa.
- **O que é enviado:** objetivo e frentes do gestor, nome, CNPJ, CNAE e município públicos, e os trechos coletados.
  **Nada da carteira.** O conteúdo coletado vai delimitado como dado não confiável.
- **Validação:** citações e contatos que não existem no texto são descartados. A IA nunca atribui confiança ALTA. Ela
  não decide CNPJ, situação, cruzamento, duplicidade, restrição nem histórico.
- **Custo:** reserva do máximo antes de cada chamada e registro do consumo medido, inclusive em respostas truncadas.
- **Sem chave:** "OpenAI aguardando configuração". As regras seguem e nunca são apresentadas como IA.

## Fluxo

```
Pesquisa (segmentos, termos, região, fontes, limites, exclusões explícitas)
  └─► Execução (fila no banco, worker em segundo plano, uma ativa por pesquisa)
        1. DESCOBERTA      Base Receita (CNPJ), OpenStreetMap/Overpass ou CSV → Candidato (fonte + id, sem duplicar)
        2. IDENTIFICAÇÃO   CNPJ da fonte ou do site público   → verificado na Receita (BrasilAPI)
                           sem CNPJ → PENDENTE + sugestões (telefone/domínio/nome), nunca fusão automática
        3. CRUZAMENTO      Empresa por CNPJ ↔ carteira Salesforce sincronizada (+ leads BRIDA)
        4. ENRIQUECIMENTO  CNAEs, abertura, situação cadastral (com referência), endereço, contatos cadastrais
        4b. COMPLEMENTAÇÃO site/canais pela internet (confere CNPJ publicado) + IA opcional
        5. EVIDÊNCIAS      regras editáveis sobre site / categoria da fonte / nome / CNAE
        6. OPORTUNIDADE    1 por empresa; qualidade × evidência × prioridade, com critérios explicados
  └─► Revisão humana → liberar (confere identidade + atualidade do cruzamento) → abordagem / retornos
```

## Situações na carteira

| Situação | Regra | Vira oportunidade? |
|---|---|---|
| Cliente da carteira | Conta BRIDA com NF de venda no período OU `LastBuyDate__c` preenchido | Não (fica marcada como conhecida) |
| Cadastro sem compra comprovada | Conta BRIDA sem nenhuma evidência de compra | Sim, tipo "cadastro sem compra" |
| Lead conhecido | CNPJ nos leads **BRIDA** (leads de outros distribuidores não são importados) | Sim, tipo "lead conhecido" |
| Nova oportunidade externa | CNPJ verificado fora da carteira e dos leads BRIDA | Sim |
| Identificação pendente | Sem CNPJ confirmado | Não (fica na Revisão) |
| Bloqueada / não contatar | `ProtheusCustomerSituation__c = Bloqueado` ou marcação "não contatar" | Não; se já existia, fica BLOQUEADA |

Existir no Salesforce não significa ter comprado. Quando a empresa tem a mesma raiz de CNPJ de um cliente
(grupo ou matriz), isso aparece como alerta, sem bloquear.

## Três dimensões separadas

1. **Qualidade dos dados** (`qualidade_dados` + pendências): CNPJ verificado, situação cadastral, abertura,
   telefone, localização.
2. **Evidência de aplicação** (`Evidencia`):
   - `CONFIRMADO_FONTE`: o site ou a categoria da fonte informa a operação;
   - `HIPOTESE`: indício pelo nome ou CNAE;
   - `INSUFICIENTE`: nada encontrado ainda.

   A aplicação dos produtos é sempre "possível" e cita somente categorias do catálogo real (as categorias
   vendidas em `TypeofProdutct__c`).
3. **Prioridade** (`prioridade` + critérios):
   - **Alta:** operação confirmada pela fonte, com canal de contato e cadastro ATIVO;
   - **Média:** há evidência, mas falta algum desses itens;
   - **Aplicação a investigar:** ainda sem evidência, **o que não significa ausência de potencial**;
   - **Verificar cadastro:** situação na Receita diferente de ATIVA.

   O gestor pode revisar a prioridade com motivo, e a revisão fica registrada.

Nenhum segmento ou CNAE é descartado automaticamente. As exclusões existem apenas se o gestor as cadastrar,
na pesquisa ou em Admin → Exclusões comerciais. O candidato excluído fica visível, com o motivo.

## Fontes

| Fonte | Tipo | Documentação / licença | Observações |
|---|---|---|---|
| OpenStreetMap – Overpass API | Descoberta | wiki.openstreetmap.org/wiki/Overpass_API · ODbL (atribuição) | Instâncias públicas: uso justo e baixo volume. Para recorrência intensa, use instância própria (troque os endpoints em Fontes) |
| BrasilAPI – CNPJ | Identificação / enriquecimento | brasilapi.com.br/docs (dados abertos da Receita) | Consulta apenas CNPJ conhecido. Não aceita ainda CNPJ alfanumérico |
| Site público da empresa | Identificação / evidência | robots.txt respeitado | Até 3 páginas por site. Guarda apenas CNPJ, telefones e e-mails publicados e trechos de até 300 caracteres |
| Importação CSV | Importação | — | Alternativa operacional. **Não é mineração automática** |

Os limites (intervalo, consultas por execução e por dia, retentativas, custo) ficam em **Fontes**. Se uma fonte
exigir credencial, cadastre só o **nome** da variável de ambiente; o valor nunca é exibido nem gravado.

## Segurança

- **URLs externas:** só http/https nas portas 80 e 443.
- **IP validado na conexão:** o endereço é conferido no momento da conexão, o que bloqueia loopback, redes
  privadas, link-local e metadados de nuvem (169.254.169.254). Redirecionamentos são revalidados.
- **Limites de leitura:** tamanho máximo da resposta e apenas conteúdo HTML ou texto.
- **Conteúdo externo:** é tratado só como texto para casamento de padrões. Nunca vira instrução.
- **IA:** o ponto de extensão em `prospeccao/services/ia.py` está **desligado**, porque não há credencial
  de IA no ambiente.
- **Salesforce:** somente leitura (`describe` e `SELECT`). Nada é enviado nem cadastrado.
- **Logs:** não registram credenciais.

## Execução

- **Pela tela:** em Pesquisa, clique em **Executar agora**. A execução entra na fila e um worker é disparado
  em processo separado (`PROSPECCAO_DISPARAR_WORKER=1`). A página da execução se atualiza sozinha.
- **Pelo terminal:** `python manage.py executar_pesquisa <id>`.
- **Worker:**
  - permanente: `python manage.py prospeccao_worker`;
  - pelo Agendador de Tarefas: `python manage.py prospeccao_worker --uma-vez`.
- **Recorrência:** fica **desligada** até o gestor marcar *Admin → Configuração de análise → agendamento
  ativo*. Além disso, cada pesquisa precisa ter `frequência (horas)` preenchida.
- **Robustez:**
  - uma execução ativa por pesquisa, garantida por restrição no banco;
  - prazo e limites por execução, com até 2 retentativas e recuo progressivo;
  - cancelamento pela tela;
  - execuções sem sinal por 10 minutos viram INTERROMPIDA;
  - cada candidato é gravado em transação própria, então uma interrupção não apaga nada;
  - `completa=True` só quando nenhuma etapa foi cortada.

## Regras de abordagem (Admin → Configuração de análise)

- **Não contatar:** bloqueia novas abordagens, sem exceção.
- **Intervalo mínimo e máximo de tentativas sem conversa:** vazio significa sem restrição. Um gestor pode
  abrir exceção com justificativa, que fica gravada na abordagem.
- **Responsável:** uma oportunidade com responsável só recebe abordagem dele ou de um gestor, e só um gestor
  reatribui. Isso impede atribuições conflitantes.
- **Liberar ou exportar:** exige CNPJ confirmado e cruzamento atual, isto é, sincronização de clientes dentro
  de `horas_validade_cruzamento` (padrão de 24 h). Fora disso, é feita uma conferência direta no Salesforce,
  somente leitura.

## Papéis de usuário e trava de atendimento

- **ADM**: vê e faz tudo. É ADM quem está no grupo "ADM" (ganha acesso total, inclusive /admin/), é superusuário ou "membro da equipe".
- **Inside Sales**: qualquer outro usuário. Vê só a tela Inside Sales (leads a procurar / procurados), a ficha do lead, o registro de contato e "Minha performance". Outras telas redirecionam para a tela dele.
- Cadastro de usuários: ADM em Gestão → Usuários e papéis (/admin/), escolhendo o grupo.
- **Trava de atendimento**: "Atender" marca o lead como em atendimento pela pessoa; os outros veem "Em atendimento: Fulano" e não abrem o registro. A marcação sai ao registrar o contato, em "Liberar"/"Cancelar e liberar" ou sozinha após `PROSPECCAO_ATENDIMENTO_MIN` minutos (padrão 30). O ADM pode liberar a de qualquer pessoa e registrar mesmo com aviso.
