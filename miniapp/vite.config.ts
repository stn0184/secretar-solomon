import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

const pkg = JSON.parse(
  readFileSync(new URL("./package.json", import.meta.url), "utf8"),
) as { version: string };

// .env один на все части проекта и лежит в корне репозитория.
// В сборку попадают только переменные VITE_* — токен бота сюда не доедет.
const envDir = fileURLToPath(new URL("..", import.meta.url));

export default defineConfig(({ mode }) => {
  // Путь, по которому приложение живёт на хостинге: на GitHub Pages это
  // `/<репозиторий>/` (techspec/07-deploy.md §7.1). Пусто — корень, как в dev.
  // Читается из окружения сборки или корневого .env, в код не попадает.
  const base = loadEnv(mode, envDir, "VITE_").VITE_BASE_PATH || "/";

  return {
    base,
    plugins: [react()],
    envDir,
    define: {
      __APP_VERSION__: JSON.stringify(pkg.version),
    },
    build: {
      outDir: "dist",
      sourcemap: true,
    },
  };
});
