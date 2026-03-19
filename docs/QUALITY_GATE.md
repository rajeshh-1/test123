# Quality Gate (Phase 3)

## Objetivo
Definir um fluxo local e de CI simples para bloquear regressao em `bot.core` sem depender de rede, segredos ou execucao live.

## Comandos locais
1. Instalar dependencias de desenvolvimento:
```bash
pip install -r requirements-dev.txt
```
2. Rodar compilacao de sintaxe (escopo relevante):
```bash
make compile
```
3. Rodar testes com cobertura:
```bash
make test
```
4. Rodar gate completo:
```bash
make check
```

Sem `make`, execute diretamente:
```bash
python scripts/quality_gate.py check
```

Ou, de forma explicita:
```bash
python -m compileall -q bot scripts tests logs/arb_engine logs/run_arb_dry_run.py logs/live_direct_arb.py logs/analyze_arb.py
python -m pytest -q
```

## Criterios de aprovacao
- `make compile` com retorno zero.
- `make test` com retorno zero.
- Cobertura minima de 80% em:
  - `bot.core.edge`
  - `bot.core.pretrade`
  - `bot.core.config`
- Nenhum teste deve depender de API externa ou credenciais reais.

## Interpretacao rapida de falhas
- Falha em compile:
  - Erro de sintaxe em arquivo Python no escopo do bot.
- Falha em test:
  - Regressao funcional em edge, pretrade ou validacao de startup.
- Falha de cobertura:
  - Testes passam, mas faltam cenarios em modulos criticos. O pytest falha por `--cov-fail-under=80`.

## CI
- Workflow: `.github/workflows/ci.yml`
- Trigger: `push` e `pull_request`
- Executa apenas fluxo paper/test (`make check`)
- Nao usa segredos.
