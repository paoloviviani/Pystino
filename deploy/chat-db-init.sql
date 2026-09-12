-- The chat's own store (ADR 0070): run once on a fresh Postgres volume.
-- The base image runs every script in /docker-entrypoint-initdb.d as the
-- bootstrap superuser, so the role and database are created here, and the
-- extensions land inside the chat's database where its tables live.
CREATE ROLE chat LOGIN PASSWORD 'change-me';
CREATE DATABASE chat OWNER chat;
