/**
 * Минимальное описание Deno — ровно то, чем пользуются наши функции.
 *
 * Edge Functions выполняются в Deno, а проверяем мы их типы с помощью tsc
 * (Deno на машине разработчика не нужен). Файл ничем не импортируется,
 * поэтому в рантайме Deno его не видит и с настоящим Deno не спорит.
 */
declare namespace Deno {
  export const env: {
    get(key: string): string | undefined;
  };

  export function serve(
    handler: (request: Request) => Response | Promise<Response>,
  ): { finished: Promise<void> };
}
