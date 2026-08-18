REGRAS OBRIGATÓRIAS DE ALTERAÇÃO DO PROJETO

1. Nunca alterar mais de 3 arquivos em uma tarefa sem autorização explícita.
2. Antes de modificar qualquer arquivo, listar:
   - arquivos que serão alterados
   - arquivos que serão criados
   - motivo de cada alteração
3. Não modificar arquivos fora da lista aprovada.
4. Não realizar refactors não solicitados.
5. Não alterar arquitetura, estratégia, risco ou fórmulas científicas sem autorização explícita.
6. Não criar arquivos temporários, dumps, backups ou snapshots no repositório.
7. Relatórios e artefatos devem ser gravados somente em:
   optimization/results/
8. Não criar um novo arquivo de relatório a cada ciclo ou execução automática.
9. Quando possível, reutilizar arquivos existentes.
10. Não executar alterações em massa.
11. Não usar scripts para modificar múltiplos arquivos sem autorização.
12. Se a solução exigir mais de 3 arquivos, parar e pedir aprovação.
13. Nunca alterar arquivos apenas para formatação, lint ou reorganização se isso não fizer parte da tarefa.
14. Antes de concluir, executar git diff --stat e informar:
    - arquivos modificados
    - arquivos criados
    - arquivos removidos
15. Se o diff exceder o escopo esperado, PARAR e não continuar.