# =====================================================================
# Frontend image — Vite dev server (with HMR) for local development.
# For production hosting you'd swap this for a `npm run build` output
# served by nginx; the dev server is fine for the local "drop bills,
# process, export" workflow.
# =====================================================================
FROM node:20-alpine

WORKDIR /app

# Install dependencies first so this layer is cached between rebuilds.
COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund

# Copy the rest of the frontend (the docker-compose mount overrides this
# at runtime in dev mode).
COPY . /app

ENV HOST=0.0.0.0
EXPOSE 5173

# `--host 0.0.0.0` is required so the port is reachable from the host
# Docker forwards. `--strictPort` makes the container fail fast if 5173
# is already taken inside the container, which is what we want.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173", "--strictPort"]
