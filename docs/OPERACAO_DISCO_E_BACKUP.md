# Disco, Docker e cópia de segurança — operação e recuperação

## O que aconteceu em 16/11/2026

- O disco **C:** (237 GB, único disco local) chegou a **78 MB livres** às 12:32. O PostgreSQL desta aplicação
  (container `brida-prospeccao-pg`) parou de gravar e o Docker Desktop deixou de responder.
- A execução 23 (teste de três cidades) foi interrompida às 12:32:37; o que já tinha sido gravado foi preservado.
- Recuperação: exclusão dos 24 arquivos `.zip` da Receita 2026-10 (6,5 GB, já importados no banco; podem ser
  baixados de novo) e reinício autorizado do Docker Desktop às 12:51. Todos os bancos voltaram aceitando conexões;
  o banco desta aplicação teve desligamento limpo (sem recuperação de falha).

### De onde vem o uso do disco

| Item (16/11) | Tamanho |
|---|---|
| Disco virtual do Docker (`%LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx`) | 47,8 GB |
| – imagens de todos os projetos | 21,6 GB |
| – volumes (bancos de todos os projetos) | 17,0 GB |
| – cache de build | 11,8 GB (2,9 GB recuperável) |
| – banco desta aplicação (`carteira`) | 1,9 GB (1,75 GB = base Receita) |
| Arquivos `.zip` da Receita (excluídos) | 6,5 GB |

O disco virtual do Docker **só cresce**: o espaço liberado dentro dele (exclusões, transações desfeitas) não volta
ao Windows. Em 16/11 os picos vieram da restauração seletiva da carteira (≈195 mil registros) e de duas simulações
que carregaram e desfizeram o mesmo volume no banco operacional, com o respectivo log de transações. O banco em si
não ficou inchado (437 linhas mortas em 1,8 milhão; o autovacuum já recolheu).

## Regras para não repetir

1. **Não fazer simulações volumosas no banco operacional.** Simulação de grande volume só em banco de teste
   (SQLite dos testes ou cópia separada).
2. **Conferência de espaço antes de executar** (já no sistema): o botão Executar bloqueia na hora com menos de
   `PROSPECCAO_DISCO_MIN_GB` (padrão 3 GB) livres; cópia de segurança e limpeza também conferem antes.
3. **Execução interrompida por disco cheio** termina como *Interrompida* com a mensagem “falta de espaço em
   disco”; o que foi gravado é mantido e o próximo Executar retoma (vinculado, sem zerar contagem nem orçamento).
4. Arquivos baixados da Receita podem ser apagados depois de importados (o banco guarda os dados).

## Cópia de segurança

```
python manage.py backup_banco --destino "F:\<pasta definida pela equipe>" --manter 7
```

- Grava `carteira-AAAAMMDD-HHMMSS.sql.gz` (pg_dump compactado) **fora do disco C:**.
- Confere espaço no destino (2× o tamanho do banco) e mantém as N cópias mais recentes.
- O destino (pasta de rede F:/M:/Y: ou disco externo) e a frequência são decisão da equipe; nada foi agendado.

## Recuperação

1. Docker sem responder: conferir espaço livre em C:. Se estiver cheio, liberar espaço primeiro.
2. Reiniciar **somente** o Docker Desktop (não reiniciar o computador, não recriar containers nem volumes).
   Isso para temporariamente todos os containers da máquina (portal, licenças, RH, etc.).
3. Depois do reinício, iniciar os containers que estavam ativos (`docker start <nome>`), conferir
   `docker exec <container> pg_isready` e o log do banco (`docker logs --since 30m brida-prospeccao-pg`).
4. Banco corrompido ou perdido (raro): criar banco vazio no mesmo container e restaurar a cópia:
   `gunzip -c carteira-AAAAMMDD.sql.gz | docker exec -i brida-prospeccao-pg psql -U <usuário> <banco>`.
5. Execuções que ficaram “Executando” sem processo ativo são marcadas como *Interrompida* (com a causa) antes
   de qualquer retomada, para não haver duas execuções simultâneas.

## Solução duradoura para o armazenamento do Docker (a decidir, não executada)

- O único disco local é C:; F:, M: e Y: são a mesma pasta de rede (não serve para o disco do Docker).
- Opções, por ordem de impacto: (a) limpar o cache de build do Docker de forma combinada com os donos dos outros
  projetos (2,9 GB recuperáveis hoje); (b) compactar o disco virtual em janela de manutenção, com cópia de todos
  os bancos antes e todos os containers parados; (c) instalar um disco local adicional e mover o
  “Disk image location” do Docker Desktop para ele. Nenhuma dessas foi executada.
