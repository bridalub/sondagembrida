# Análise da automação (Excel.py) e mapeamento de campos

Arquivos disponíveis: apenas `Excel.py`. **Não estavam no projeto** o `config_reports.json` (lista de
relatórios/pastas) nem o `env.env` (credenciais). Por isso não se sabe quais relatórios estão
ativos hoje; a análise usa as funções SOQL do script. A automação **não foi executada** e nada
foi consultado ou alterado no Salesforce.

## 1. Fontes e objetos consultados

| Função no Excel.py | Objeto base (SOQL) | Filtros relevantes |
|---|---|---|
| `extrair_cliente_soql` | **Account** | `Distributor__r.Name LIKE '%BRIDA%'` e `ProtheusCustomerSituation__c IN ('Ativo','Inativo')` |
| `extrair_vendas_soql` | **InvoiceItem__c** → `Invoice__r` (Invoice__c) | Saída, `Status__c != 'Cancelada'`, `EmissionDate__c >= 2026-04-01`, lista de CFOPs de venda |
| `extrair_devolucao_soql` | InvoiceItem__c | Entrada, não cancelada, **sem filtro de data** |
| `extrair_pedido_soql`, `extrair_status_pedido_soql`, `extrair_pendencias*_soql` | OpportunityLineItem → Opportunity | `IsChildOrder__c` pai/filho, estágios, datas variadas |
| `extrair_inadimplencia_renato_soql`, `extrair_titulos_soql` | Titles__c | Vencidos / desde 2025-01-01, tipos NF/FT |
| `extrair_visitas_soql`, `extrair_visitas_futuras_soql` | Visit__c (+Checkins__r) | Visitas da equipe desde 2026-04-01 / 2026-06-01 |
| `extrair_vendedor_soql`, `extrair_email_vendedores_soql` | Seller__c, User | Equipe interna BRIDA |
| `extrair_produto_soql`, `extrair_fator_conversao_produto_soql`, `extrair_estoque_soql`, `extrair_spl_*` | Product2, Storage__c, SinglePriceListItem__c | Catálogo/estoque/preço |
| `extrair_pesquisa_preco_soql` | SurveyAnswerItem__c | Pesquisa de preço desde 2026-04-01 |
| `extrair_aprovacoes_pendentes_soql` | ProcessInstanceWorkitem/Node | Aprovações pendentes |

## 2. Relacionamentos confirmados

- **Empresa** = `Account`. Identificador da conta: `Id`. Ele **não era consultado** pela automação e foi
  acrescentado (campo-padrão de todo objeto).
- **Compra** = `InvoiceItem__c` → `Invoice__r` (nota) → `Invoice__r.Customer__r` (Account). A
  automação lia `Invoice__r.Customer__r.Name`; aqui lemos `Invoice__r.Customer__r.Id` para ligar a nota
  à conta pelo Id, não pelo nome.
- **Pedido** = `OpportunityLineItem` → `Opportunity` → `Opportunity.Account`. Há pedido pai e pedidos
  filhos (`IsChildOrder__c`, `ParentOrder__c`).
- **Pessoas de contato do cliente: nenhuma consulta existe.** `Seller__c` e `User` são vendedores e
  usuários internos, não contatos do cliente. O objeto `Contact` não aparece no script.

## 3. CNPJ

Campo `Account.CNPJ__c`. O script não trata esse valor. Não se sabe se o campo é texto ou número, nem
se pode conter CPF. Daí as regras aplicadas: normalizar, validar o DV (inclusive no formato
alfanumérico), detectar CPF e restaurar zeros à esquerda só quando o DV confere.

## 4. Evidência de compra (regra adotada)

**Compra comprovada = nota fiscal de SAÍDA, não cancelada (`Status__c != 'Cancelada'` e
`IsInvoiceCanceled__c = false`), com CFOP de venda** (a mesma lista `CFOPS_VENDAS` do relatório VENDAS).

Oportunidade/pedido **não** conta como compra. Os estágios vistos no script (`Orçamento`,
`Em Aprovação`, `Reprovado`, `Cancelado`, `Faturado`) mostram que um pedido pode não virar venda. Além
disso, pai e filho somados contariam o mesmo pedido duas vezes. As devoluções (NF de Entrada) são
guardadas à parte e não anulam a classificação de compra.

## 5. Período coberto

- Vendas: a partir de **01/04/2026**, filtro fixo no relatório VENDAS. O sistema usa a mesma data como
  padrão (`COMPRAS_DESDE_PADRAO`), configurável com `--desde`.
- Não se sabe se o Salesforce tem notas anteriores a essa data. **Por isso "sem compra" quer dizer
  "sem compra no período"**, e a classificação "Nunca comprou" vem **desligada**. Ela só pode ser
  ligada depois de confirmar que as notas anteriores ao cadastro da conta estão integradas.
- `Account.CreatedDate` é a data de cadastro no Salesforce, **não** a data de abertura da empresa nem
  o início como cliente.

## 6. Filtros que limitam o resultado

- Apenas contas com distribuidor contendo "BRIDA" e situação Protheus **Ativo/Inativo**. Contas com
  outras situações (por exemplo, prospects ou bloqueadas) ficam de fora. Configurável por
  `SF_SITUACOES_CLIENTE`.
- Notas de contas fora desse filtro são guardadas sem empresa e aparecem como inconsistência.
- A lista de CFOPs inclui códigos de entrada (1xxx/2xxx/3xxx), que nunca combinam com `Type__c='Saída'`.
  Eles não causam efeito, mas mostram que a lista foi copiada de outro relatório.

## 7. Problemas encontrados no script (não reaproveitados)

- `aplicar_formato_datas` **preenche datas vazias com 01/01/2000**. Isso é um dado falso e não foi
  reaproveitado; aqui uma data vazia fica vazia.
- Detecção de colunas de data pelo formato do valor, não pelo tipo do campo.
- VENDAS/DEVOLUÇÃO multiplicam linhas pela subconsulta `ScoreGoal__r`, uma por pontuação. Somar
  "Valor Total" do Excel gera **duplicidade**. Aqui os valores são agregados por nota, sem essa
  subconsulta.
- O ajuste de largura de colunas está fora do loop (só a última coluna é ajustada). Isso é irrelevante
  para este sistema.
- Nos pedidos, os filtros mudam entre relatórios (pai/filho, datas fixas diferentes).
- Datas e horários: o script converte UTC para UTC-3 fixo. Aqui a conversão usa o fuso
  America/Sao_Paulo do Django.

## 8. Mapeamento dos campos

### Account (empresa)

| Campo de origem | Significado (rótulo no script) | Finalidade no projeto | Decisão |
|---|---|---|---|
| `Id` | Id da conta (não era consultado) | Rastreabilidade, chave de vínculo | **Aproveitar** (acrescentado) |
| `CNPJ__c` | CNPJ | Chave de negócio | **Aproveitar** (normalizado + valor bruto guardado) |
| `Name` | Nome da conta | Nome exibido. Não se sabe se é razão social ou fantasia | **Aproveitar** como "nome da conta" |
| `Code__c` | Código do cliente (Protheus) | Busca/conferência | **Aproveitar** |
| `CustomizedID__c` | ID customizado | Rastreabilidade | **Aproveitar** |
| `Segment__c` / `Subsegment__c` | Segmento / Subsegmento | Segmento comercial (separado do CNAE) | **Aproveitar** |
| `Metier__c` | Ramo | Segmentação | **Aproveitar** |
| `IMBP__c` | IMBP (significado não documentado) | Segmentação | **Aproveitar** (exibido como está) |
| `ConsumptionClassification__c` | Consumo/Indústria | Segmentação | **Aproveitar** |
| `IsGrainy__c` | Granel | Característica de compra | **Aproveitar** |
| `BillingCity__c` / `BillingState__c` / `BillingPostalCode` | Cidade/UF/CEP de faturamento | Localização (filtros) | **Aproveitar** |
| `ShippingCity__c` / `ShippingState__c` | Cidade/UF de entrega | Referência | **Aproveitar** (só exibição) |
| `Zone__c` | Região | Filtro/exibição | **Aproveitar** |
| `Seller__r.Name` | Vendedor | Responsável comercial | **Aproveitar** |
| `CoordinatorFormula__c` | Coordenador | Responsável comercial | **Aproveitar** |
| `ProtheusCustomerSituation__c` | Situação Protheus | Contexto | **Aproveitar** |
| `CreatedDate` | Data de cadastro no SF | Só para a regra de cobertura ("nunca comprou") | **Aproveitar** (rotulado como cadastro no SF) |
| `Distributor__r.Name` | Distribuidor | Rastreabilidade do filtro | **Aproveitar** |
| `Id_do_vendedor__c` | Id do vendedor | Não agrega à análise | Descartar |
| `BillingStreet`, `BillingStreet__c`, `BillingAddressContinued__c`, `BillingNeighborhoodTxt__c`, `ShippingStreet`, `ShippingNeighborhood__c`, `ShippingPostalCode__c` | Endereço completo | Não é necessário para segmentação (minimização de dados) | Descartar |
| `ShippingLatitude__c` / `ShippingLongitude__c` | Coordenadas | Fora do escopo desta fase | Descartar |

### Campos desejados que a automação **não** traz

| Desejado | Situação | Como o sistema trata |
|---|---|---|
| Telefone comercial | Não consultado. `Phone` é campo-padrão de Account | Lido só se o `describe` confirmar a existência (`SF_ACCOUNT_CAMPOS_EXTRAS=telefone=Phone`) |
| E-mail comercial, CNAE, data de abertura, razão social, nome fantasia | **Não existem na automação** | Campos ficam "desconhecido". A importação lista os **candidatos** encontrados no `describe` (por nome/rótulo) para alguém confirmar e mapear em `SF_ACCOUNT_CAMPOS_EXTRAS` |
| Pessoas de contato (nome, cargo, telefone, e-mail) | **Não existem na automação** | Cadastro manual na tela da empresa. Uma importação de `Contact` depende de confirmar o objeto e os campos |

### InvoiceItem__c / Invoice__c (compras)

| Campo de origem | Significado | Finalidade | Decisão |
|---|---|---|---|
| `Invoice__r.Id`, `Invoice__r.Name` | Nota e número | Agrupar itens por nota, rastrear | **Aproveitar** |
| `Invoice__r.Customer__r.Id` | Conta da nota | Ligar compra à empresa | **Aproveitar** (o script usava só o Name) |
| `Invoice__r.Type__c`, `Status__c`, `IsInvoiceCanceled__c`, `CodeFiscalOperation__c` | Tipo/status/CFOP | Regra de compra comprovada | **Aproveitar** como filtro |
| `EmissionDate__c` | Data de emissão | Primeira/última compra | **Aproveitar** |
| `ValorTotalFormula__c` | Valor total do item | Valor acumulado | **Aproveitar** (o script não documenta se inclui impostos) |
| `TypeofProdutct__c` | Tipo de produto | Categorias compradas (segmentação) | **Aproveitar** (agregado por nota) |
| Demais campos (custos, margens, SPL, impostos, pontuação, vendedor da nota, pedido pai/filho) | Comercial/comissão | Não necessários para os indicadores | Descartar (o detalhe por item não é importado) |

### Outros objetos

| Objeto | Decisão | Motivo |
|---|---|---|
| Opportunity/OpportunityLineItem | Descartar nesta fase | Pedido não é compra concluída |
| Titles__c | Descartar nesta fase | Financeiro/inadimplência; pode servir depois como restrição de abordagem |
| Visit__c | Descartar nesta fase | Visitas da equipe interna, não abordagens de prospecção. Pode indicar empresa já atendida |
| Seller__c / User | Descartar | Equipe interna (o nome do vendedor já vem pela Account) |
| Product2, Storage__c, SinglePriceListItem__c, SurveyAnswerItem__c, ProcessInstance* | Descartar | Sem relação com a análise da carteira |

## 9. Dados ausentes, duplicados ou inconsistentes (tratamento)

- Conta sem CNPJ, com CNPJ inválido ou com CPF: vira empresa própria com **revisão**. Não é associada
  a nenhuma outra por nome.
- Mesmo CNPJ em mais de uma Account: **uma empresa**, várias origens registradas, dados da primeira
  conta (principal) e revisão sinalizada.
- CNPJ que muda ou some na origem: o valor anterior é mantido e a mudança é registrada como
  inconsistência.
- Nota de conta não importada: guardada sem empresa e vinculada automaticamente quando a conta for
  importada.
- Nota que deixa de vir na origem dentro do período (por exemplo, cancelada depois): marcada
  `presente_na_origem = False`, sai dos indicadores e **não é apagada**.
- Item sem valor: somado como 0, com aviso.
