import { Empty } from "./Empty.tsx";
import { Header } from "./Header.tsx";

/**
 * Вкладка «Сферы» (`techspec/32-redesign.md` §32.2). Сфер нет — одобренное
 * пустое состояние (`design.md` §2, артборд 09): пунктирное место под первую
 * сферу и как их назвать. Внизу — карточка «Без сферы и о себе»: вход в
 * «О себе», отдельной вкладки у него больше нет.
 */
export function SpheresTab({
  onClose,
  onOpenAbout,
}: {
  onClose: () => void;
  onOpenAbout: () => void;
}) {
  return (
    <main className="screen screen--tabs">
      <Header eyebrow="Ваши сферы" title="Сферы" />
      <Empty
        blank={
          <div className="blank">
            <b>Первая сфера</b>
            Появится здесь, когда вы назовёте её в чате
          </div>
        }
        title="Сфер пока нет"
        text="Назовите в чате свои сферы — бизнесы, семью, интересы. Соломон будет раскладывать по ним дела, переписку и часы."
        examples={["Мои сферы: работа, семья, спорт", "Это по работе: отвечаю за продажи"]}
        hint="Сфер может быть до 12. Дело без сферы — тоже нормально."
        onClose={onClose}
      />
      <button type="button" className="sph sph--plain" onClick={onOpenAbout}>
        <span className="sph__top">
          <span className="sph__name">Без сферы и о себе</span>
          <span className="chev" aria-hidden="true">
            ›
          </span>
        </span>
      </button>
    </main>
  );
}
