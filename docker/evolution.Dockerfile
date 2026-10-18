FROM node:24-bookworm-slim AS builder

ARG EVOLUTION_REVISION=cd800f2976e1e5b682fbf86a01ee4d85ae61f370
ENV HUSKY=0 DATABASE_PROVIDER=postgresql
WORKDIR /evolution
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates openssl python3 make g++ \
    && rm -rf /var/lib/apt/lists/*
RUN git init . \
    && git remote add origin https://github.com/evolution-foundation/evolution-api.git \
    && git fetch --depth 1 origin "$EVOLUTION_REVISION" \
    && git checkout --detach FETCH_HEAD
RUN npm ci --no-audit --no-fund \
    && npm install --save-exact baileys@7.0.0-rc14 --no-audit --no-fund \
    && npm install --save-dev --save-exact patch-package@8.0.1 --no-audit --no-fund
COPY patches/baileys+7.0.0-rc14.patch ./patches/
RUN npx --no-install patch-package \
    && npm run db:generate \
    && npm run build
COPY tests/test_evolution_pairing.mjs /checks/test_evolution_pairing.mjs
RUN EVOLUTION_HOME=/evolution node --test /checks/test_evolution_pairing.mjs \
    && cp -r prisma/postgresql-migrations prisma/migrations \
    && npm prune --omit=dev --ignore-scripts --no-audit --no-fund

FROM node:24-bookworm-slim
ENV NODE_ENV=production TZ=Asia/Kolkata
WORKDIR /evolution
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates openssl ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /evolution/node_modules ./node_modules
COPY --from=builder /evolution/package.json ./package.json
COPY --from=builder /evolution/dist ./dist
COPY --from=builder /evolution/prisma ./prisma
COPY --from=builder /evolution/public ./public
COPY --from=builder /evolution/manager ./manager
COPY docker/evolution-entrypoint.sh /usr/local/bin/evolution-entrypoint
RUN mkdir instances store && chown node:node instances store \
    && chmod 755 /usr/local/bin/evolution-entrypoint
USER node
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/evolution-entrypoint"]
