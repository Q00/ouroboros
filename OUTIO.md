# OutioCode

> Agent de code autonome qui planifie, édite, exécute et se corrige.
> Branché sur ta plateforme [Outio](https://outio.app) — tes clés, tes
> crédits, tes modèles. Aucune fuite vers OpenAI/Anthropic en direct.

OutioCode est un fork rebrandé d'[Ouroboros](https://github.com/Q00/ouroboros)
(MIT). Le moteur agentique est identique ; ce qui change, c'est la
couche de routage des modèles — toutes les requêtes LLM passent par
l'endpoint OpenAI-compatible d'Outio (`/api/v1/chat/completions`).

## Installation

```bash
pipx install outio-code           # recommandé (isolé)
# ou
pip install outio-code             # dans ton venv courant
```

## Configuration (30 secondes)

1. Va sur [outio.app/dashboard/settings?tab=integrations](https://outio.app/dashboard/settings?tab=integrations)
   et crée une clé API. Elle commence par `outio_sk_…`.
   *Nécessite un forfait Business* (l'API programmatique est gated).

2. Exporte la clé dans ton shell :

   ```bash
   export OUTIO_API_KEY="outio_sk_..."
   ```

   Optionnel — si tu utilises une instance Outio self-hosted ou un
   environnement non-prod :

   ```bash
   export OUTIO_API_BASE="https://outio.app/api/v1"  # défaut
   ```

3. C'est tout. Le défaut runtime est `outio` — aucune autre config requise.

## Premier run

```bash
outio-code init                              # initialise un nouveau projet
outio-code run seed.yaml                     # exécute le seed
outio-code run seed.yaml --model claude-sonnet-4-5
```

Les `--model` acceptent les **slugs internes Outio** (`claude-sonnet-4-5`,
`gpt-4o`, `gemini-2.5-pro`, etc.). Le catalogue complet est sur
[outio.app/admin/models](https://outio.app/admin/models) si tu es admin,
ou via l'API `GET https://outio.app/api/models`.

## Coûts

Chaque appel LLM débite tes crédits Outio selon les tarifs admin :
- 1–30 crédits par 1M tokens prompt
- 1–30 crédits par 1M tokens completion
- Plancher 1 crédit par requête

Tu peux suivre ta consommation en temps réel sur
[outio.app/dashboard/plans](https://outio.app/dashboard/plans) ou via
le champ `outio.creditsDebited` que renvoie chaque réponse.

## Backends alternatifs

Si tu préfères passer par ton compte Claude Code, Codex, Gemini CLI,
Copilot CLI, Goose ou un LiteLLM en direct, change la variable d'env :

```bash
export OUROBOROS_RUNTIME=claude_code   # ou litellm, codex, gemini_cli…
```

Tous les backends Ouroboros upstream restent disponibles — OutioCode
ajoute juste le backend `outio` et en fait le défaut.

## Licence

MIT, comme Ouroboros upstream. Le code applicatif (Outio platform)
reste propriétaire à Outio ; le CLI lui-même est libre.

## Crédits

- Moteur agentique : [Ouroboros](https://github.com/Q00/ouroboros) par Q00 (MIT)
- Plateforme + intégration : [Outio](https://outio.app)
