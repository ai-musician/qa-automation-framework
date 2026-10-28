# Two images from one file:
#   api    the mock fintech API (Flask only)
#   tests  pytest + Playwright with the browsers already installed
# Built and run together by docker-compose.yml.

FROM python:3.12-slim AS api
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir "$(grep -i '^flask==' requirements.txt)"     # same Flask version as the tests use
COPY mock_server/ mock_server/
ENV HOST=0.0.0.0 PORT=5050
EXPOSE 5050
HEALTHCHECK --interval=2s --timeout=2s --retries=15 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5050/')"
CMD ["python", "-m", "mock_server.app"]


# Microsoft's Playwright image: the same version as in requirements.txt, so its browsers match
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble AS tests
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# the API runs in its own container, so the tests don't start one
ENV START_MOCK_SERVER=false
# -n auto: run the tests in parallel, one worker per CPU core (pytest-xdist; load tests stay in one process)
CMD ["pytest", "-n", "auto"]
