"""Run durable business/Agent work independently from HTTP and solver processes."""

import argparse
import logging
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

from sqlalchemy.engine import Engine

from packages.agent.case_runtime import process_case
from packages.agent.cases import ingest_sources, wake_completed_jobs
from packages.agent.human_tasks import tick_reminders
from packages.agent.work import process_one
from packages.integrations.factory_http import FactoryControls, FactoryExecution, FactoryHTTP
from packages.integrations.notifications import deliver_notification
from packages.persistence import connect
from packages.planning.publication import deliver_one
from packages.planning.service import synchronize_due
from packages.providers.catalog import ModelCatalog
from packages.providers.registry import ProviderUnavailable, TextModel, configured_model
from packages.providers.selection import UserModelRouter
from packages.settings import Settings

logger = logging.getLogger(__name__)


class UnavailableModel:
    def complete(self, prompt: str) -> str:
        raise ProviderUnavailable("MODEL_NOT_CONFIGURED")


def poll_business(
    engine: Engine,
    connector: FactoryHTTP,
    writer: FactoryExecution | None,
    controls: FactoryControls | None = None,
) -> bool:
    synchronize_due(engine, connector)
    ingest_sources(engine)
    wake_completed_jobs(engine)
    tick_reminders(engine)
    if controls is not None:
        from packages.agent.assistant import process_one as process_assistant
        from packages.agent.treatment_execution import process_treatment

        process_treatment(engine, connector, controls)
        process_assistant(engine, connector, controls)
    return deliver_one(engine, connector, writer) if writer is not None else False


def run_agent_step(settings: Settings, engine: Engine, model: TextModel) -> bool:
    agent_source = FactoryHTTP(
        settings.factory_api_url, settings.factory_api_token.get_secret_value()
    )
    try:
        return process_case(engine, agent_source, model) or process_one(engine, agent_source, model)
    finally:
        agent_source.close()


def poll_loop(
    poll: Callable[[], bool],
    agent_step: Callable[[], bool],
    *,
    once: bool = False,
    should_stop: Callable[[], bool] | None = None,
    pause: Callable[[float], None] = time.sleep,
    mail_step: Callable[[], bool] | None = None,
) -> None:
    with ExitStack() as pools:
        agents = pools.enter_context(
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="byof-agent")
        )
        mail = (
            pools.enter_context(ThreadPoolExecutor(max_workers=1, thread_name_prefix="byof-mail"))
            if mail_step
            else None
        )
        running = None
        sending = None
        while should_stop is None or not should_stop():
            delivered = poll()
            if sending is not None and sending.done():
                try:
                    sending.result()
                except Exception:
                    logger.error("NOTIFICATION_TASK_FAILED")
                sending = None
            if mail is not None and mail_step is not None and sending is None:
                sending = mail.submit(mail_step)
            if running is not None and running.done():
                try:
                    running.result()
                except Exception:
                    # Persisted Case/operation state owns recovery, not an untracked model retry.
                    logger.error("AGENT_TASK_FAILED")
                running = None
            if running is None:
                running = agents.submit(agent_step)
            if once:
                try:
                    running.result()
                except Exception:
                    logger.error("AGENT_TASK_FAILED")
                if sending is not None:
                    try:
                        sending.result()
                    except Exception:
                        logger.error("NOTIFICATION_TASK_FAILED")
                return
            if not delivered:
                pause(1)


def run_worker(
    settings: Settings,
    *,
    once: bool = False,
    should_stop: Callable[[], bool] | None = None,
    pause: Callable[[float], None] = time.sleep,
) -> None:
    try:
        model = configured_model(settings)
    except ProviderUnavailable:
        model = UnavailableModel()
    model = UserModelRouter(ModelCatalog(settings), model)
    with ExitStack() as resources:
        engine = connect(settings.database_url.get_secret_value())
        resources.callback(engine.dispose)
        connector = FactoryHTTP(
            settings.factory_api_url, settings.factory_api_token.get_secret_value()
        )
        resources.callback(connector.close)
        writer = None
        controls = None
        if settings.factory_control_token.get_secret_value():
            controls = FactoryControls(
                settings.factory_api_url, settings.factory_control_token.get_secret_value()
            )
            resources.callback(controls.close)
        if settings.factory_execution_token.get_secret_value():
            writer = FactoryExecution(
                settings.factory_api_url, settings.factory_execution_token.get_secret_value()
            )
            resources.callback(writer.close)
        poll_loop(
            lambda: poll_business(engine, connector, writer, controls),
            lambda: run_agent_step(settings, engine, model),
            once=once,
            should_stop=should_stop,
            pause=pause,
            mail_step=(lambda: deliver_notification(engine, settings))
            if settings.smtp_mode != "disabled"
            else None,
        )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    run_worker(Settings(), once=args.once)


if __name__ == "__main__":
    main()
