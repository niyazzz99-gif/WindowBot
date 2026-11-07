from aiogram import Router

from bot.handlers import common, tasks


def build_router() -> Router:
    root = Router(name="root")
    # common первым: команды должны срабатывать в любом состоянии FSM
    root.include_routers(common.build_router(), tasks.build_router())
    return root
