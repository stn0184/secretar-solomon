import { formatDay } from "../lib/format.ts";
import { Empty } from "./Empty.tsx";
import { Header } from "./Header.tsx";

/**
 * Вкладка «Сегодня» (`techspec/32-redesign.md` §32.2). Итог дня, доли сфер и
 * нить дня строит этап 035 (§32.3); до него — одобренное пустое состояние
 * (`design.md` §2, артборд 07): без крупной цифры и полосы долей, только
 * надзаголовок с датой, объяснение, два примера и кнопка в чат.
 */
export function TodayTab({ now, onClose }: { now: Date; onClose: () => void }) {
  return (
    <main className="screen screen--tabs">
      <Header eyebrow={`Сегодня · ${formatDay(now)}`} />
      <Empty
        title="Сегодня свободно"
        text="Ни встреч, ни дел на сегодня. Часы появятся сами — из встреч с длительностью и из переписки в личных чатах."
        examples={["Созвон с Игорем завтра с 14 до 16", "Тренировка в субботу в 9"]}
        hint="Напишите или наговорите боту — дело встанет в ленту."
        action="Написать Соломону"
        onClose={onClose}
      />
    </main>
  );
}
