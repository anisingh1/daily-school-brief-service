#!/bin/sh
set -eu

./node_modules/.bin/prisma migrate deploy --schema ./prisma/postgresql-schema.prisma
exec node dist/main.js
