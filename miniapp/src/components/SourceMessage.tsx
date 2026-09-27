import { formatMoment } from "../lib/format.ts";
import type { SourceMessage as Source } from "../lib/tasks.ts";

/**
 * Исходное сообщение целиком — видно, что именно помощник читал.
 * Запись без источника или сообщение, которого не осталось, — короткая
 * подпись вместо цитаты.
 *
 * `compact` — внутри раскрытой записи памяти: без своей секции и карточки,
 * только заголовок с датой и цитата; `tone="guess"` красит кромку цветом
 * предположения — из этого сообщения помощник вывел, а не прочитал.
 */
export function SourceMessage({
  message,
  now,
  compact = false,
  tone,
}: {
  message: Source | null;
  now: Date;
  compact?: boolean;
  tone?: "guess";
}) {
  const box = tone === "guess" ? "source__box source__box--guess" : "source__box";

  if (compact) {
    return message ? (
      <div className="source source--inline">
        <div className="source__top">
          <span>Исходное сообщение</span>
          <span>{formatMoment(message.receivedAt, now)}</span>
        </div>
        <blockquote className={box}>
          <p className="source__text">{message.text}</p>
        </blockquote>
      </div>
    ) : (
      <p className="note">Исходного сообщения нет.</p>
    );
  }

  return (
    <section className="sec">
      <h2 className="sec__h">
        <span>Исходное сообщение</span>
      </h2>
      {message ? (
        <div className="card">
          <div className="source">
            <div className="source__top">
              <span>Текст</span>
              <span>{formatMoment(message.receivedAt, now)}</span>
            </div>
            <blockquote className={box}>
              <p className="source__text">{message.text}</p>
            </blockquote>
          </div>
        </div>
      ) : (
        <p className="note">Исходного сообщения нет.</p>
      )}
    </section>
  );
}
