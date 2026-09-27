FROM node:24.15.0-bookworm-slim AS dependencies
WORKDIR /app
RUN npm install --global npm@11.1.0
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --no-fund
COPY apps/web/ ./

FROM dependencies AS development
USER node
EXPOSE 8080
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "8080"]

FROM dependencies AS build
RUN npm run build

FROM nginx:1.30.5-alpine AS runtime
COPY deploy/nginx.conf /etc/nginx/nginx.conf
COPY --from=build /app/dist /usr/share/nginx/html
USER nginx
EXPOSE 8080
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
