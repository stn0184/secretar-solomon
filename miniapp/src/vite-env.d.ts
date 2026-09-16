/// <reference types="vite/client" />

/** Версия приложения — подставляется сборкой из package.json. */
declare const __APP_VERSION__: string;

interface ImportMetaEnv {
  /** Адрес проекта Supabase. Публичная переменная — см. инвариант 1 в CLAUDE.md. */
  readonly VITE_SUPABASE_URL?: string;
  /** Анонимный ключ Supabase: публичный по замыслу, данные защищают правила доступа. */
  readonly VITE_SUPABASE_ANON_KEY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
