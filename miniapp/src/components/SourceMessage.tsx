import { formatMoment } from "../lib/format.ts";
import type { SourceMessage as Source } from "../lib/tasks.ts";

/**
 * Исходное сообщение целиком — видно, что именно помощник читал.
 * Задача без источника или сообщение, которого не осталось, — короткая
 * подпись вместо цитаты.
 */
export function SourceMessage({ message, now }: { message: Source | null; now: Date }) {
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
            <blockquote className="source__box">
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
