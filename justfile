set dotenv-filename := ".env.local"

marimo-exp:
    uv run marimo edit test.py

run script:
    uv run {{ script }}

# Fast local tests (subprocess backend only — no API keys needed)
test:
    uv run pytest src -q -m "not e2e"

# End-to-end tests against real Daytona sandboxes (needs DAYTONA_API_KEY)
e2e:
    uv run pytest src/test_e2e_daytona.py -q

