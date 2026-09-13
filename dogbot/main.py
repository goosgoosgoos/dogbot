import asyncio
from .config import *
from .bot import DogBot

def main():

    if not PASSWORD:

        raise SystemExit(
            "Не задан DOG_BOT_PASSWORD."
        )

    if not OPENROUTER_KEY:

        raise SystemExit(
            "Не задан OPENROUTER_KEY."
        )

    xmpp = DogBot(
        JID,
        PASSWORD,
        ROOM,
        NICK,
        OPENROUTER_KEY,
        OPENROUTER_MODEL,
    )

    xmpp.use_ipv6 = False

    logging.info(
        "[START] JID=%s ROOM=%s MODEL=%s PROXY=%s",
        JID,
        ROOM,
        OPENROUTER_MODEL,
        TOR_PROXY,
    )

    try:

        xmpp.connect(
            (
                "jajaba.ru",
                5222,
            )
        )
        
        xmpp.loop.run_forever()

    except KeyboardInterrupt:

        logging.info(
            "[STOP] Остановлено пользователем."
        )

    except Exception:

        logging.exception(
            "[STOP] Критическая ошибка."
        )

        raise


if __name__ == "__main__":
    main()
    
    
