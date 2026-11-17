# GitHub repo digest — Mistral Workflows

Deux workflows Mistral qui récapitulent l'activité d'un dépôt GitHub et publient le digest dans Microsoft Teams :

- **`github-repo-digest`** (OBO) — à lancer manuellement. Agit au nom de l'utilisateur déclencheur : lit ses chats Teams et publie le digest dans sa conversation « avec soi-même ». Publie via le connecteur `teams` ; nécessite un déploiement **hardened**.
- **`github-repo-digest-scheduled`** — pour exécution planifiée (cron). Publie via l'API Microsoft Graph en mode *application* ; nécessite les secrets workspace liés au déploiement :
  - `TEAMS_TENANT_ID`, `TEAMS_CLIENT_ID`, `TEAMS_CLIENT_SECRET` (app Entra avec permission application `Chat.ReadWrite.All`, consentement admin requis),
  - `TEAMS_SENDER_UPN` (UPN du propriétaire du chat).

Optionnel : `GITHUB_TOKEN` (secret workspace) pour relever la limite de débit de l'API GitHub sur les dépôts privés ou volumineux.

## Entrées

| Champ | Défaut | Description |
|---|---|---|
| `repo` | `ThibaultHospital/cv` | Dépôt GitHub `propriétaire/nom` |
| `since` | `05/10/2026` | Date de départ, format `JJ/MM/AAAA` ou `AAAA-MM-JJ` |
| `dry_run` | `false` | Construit le digest sans le publier dans Teams |

Le digest liste, avec un résumé d'une ligne et un lien par pull request :

- les PR **fusionnées** depuis la date,
- les PR **ouvertes en attente de revue** (non approuvées).

## Structure

```
deploy/docker/Dockerfile.worker   # image du worker (déploiement géré)
src/worker/entrypoints/worker.py  # point d'entrée : découverte + run_worker
src/worker/workflows/             # définitions des workflows
tests/                            # tests pytest (environnement Temporal embarqué)
```

## Développement

```bash
uv sync
uv run pytest
```

Lancer un worker local (nécessite `MISTRAL_API_KEY` dans `.env`) :

```bash
uv run python -m worker.entrypoints.worker
```

## Déploiement

Le déploiement géré Mistral Cloud build `deploy/docker/Dockerfile.worker` depuis la racine du dépôt. Le workflow OBO doit être déployé avec `hardened=true`.
