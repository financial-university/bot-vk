import asyncio
import datetime
import random
import logging
from asyncio import AbstractEventLoop
from re import match
from urllib.parse import urlencode

import ujson
from aiovk import API
from aiovk.sessions import BaseSession
from pymysql import OperationalError

from app.longpoll import BotsLongPoll
from app.dependency import connection
from app.models import User, UserProxy
import app.utils.constants as const
from app.ruz.server import format_schedule, get_group, get_teacher
from app.utils import strings
import app.utils.keyboards as keyboards

log = logging.getLogger(__name__)


class BotResponse(dict):
    def __init__(self, **kwargs):
        super().__init__(kwargs)

    def __getattr__(self, item):
        return self[item]

    def __repr__(self):
        return f'BotResponse "{self}"'


def get_random_id():
    """ Get random int32 number (signed) """
    return random.getrandbits(31) * random.choice([-1, 1])


class Bot:
    def __init__(
        self,
        session: BaseSession,
        group_id: str = None,
        loop: AbstractEventLoop = None,
        db: connection = None,
        mode=4096,
        without_longpool=False,
    ):
        if db is None and not without_longpool:
            raise RuntimeError("DB must be set")
        self.vk = API(session)
        if not without_longpool:
            self.longpool = BotsLongPoll(session, group_id=group_id)
        else:
            self.longpool = None
        self.loop = loop or asyncio.get_running_loop()
        self.db = db

    @classmethod
    def without_longpool(
        cls, session: BaseSession, loop: AbstractEventLoop = None, db: connection = None
    ):
        return cls(session, loop=loop, without_longpool=True, db=db)

    @staticmethod
    def parse_resp(resp):
        return (
            BotResponse(**update["object"])
            for update in resp["updates"]
            if update["type"] == "message_new"
        )

    async def update_user(self, user_id, data: dict):
        async with self.db() as conn:
            await conn.execute(User.update_user(user_id, data=data))

    async def main_loop(self):
        if self.longpool is None:
            raise NotImplementedError()
        for event in self.parse_resp(await self.longpool.wait()):
            log.debug('User %s with message "%s"', event.peer_id, event.text)
            self.loop.create_task(self.handle_new_message(event))

    async def handle_new_message(self, msg: BotResponse):
        try:
            async with self.db() as conn:
                user = await (
                    await conn.execute(User.search_user(msg.peer_id))
                ).fetchone()
                if user is None:
                    await conn.execute(User.add_user(msg.peer_id))
                    user = UserProxy(dict(id=msg.peer_id))
                else:
                    user = UserProxy(user)
        except OperationalError:
            await self.send_msg(
                msg.peer_id,
                "У нас что-то пошло не по плану, попробуй написать позже...",
            )
        log.debug("New %r", user)
        payload = ujson.loads(msg.payload if "payload" in msg else "{}")
        message = msg.text.lower()

        if (
            message == ("начать" or "start" or "сброс")
            or payload.get("command", "") == "start"
        ):
            await self.send_schedule_menu(user)
        elif user.update == "2":
            await self.user_update_2(user)
        elif const.PAYLOAD_MENU in payload:
            menu = payload[const.PAYLOAD_MENU]
            if menu in const.MENUS_LIST:
                await getattr(self, menu)(user, payload=payload)
            else:
                log.warning("unexpected payload %s , user %s", payload, user.id)
                await self.send_schedule_menu(user)
        elif const.PAYLOAD_MENU not in payload:
            if user.current_name == const.CHANGES:
                if user.role == const.ROLE_STUDENT:
                    await self.send_check_group(user, message)
                elif user.role == const.ROLE_TEACHER:
                    await self.search_teacher_to_set(user, message)
                else:
                    await self.send_msg(
                        user.id,
                        "Сделайте выбор кнопками внизу\n\nЕсли кнопки не отображаются - воспользуйтесь "
                        "официальным клиентом ВКонтакте последней версии",
                    )
            elif user.found_name == const.CHANGES and user.found_id == "0":
                if user.found_type == const.ROLE_TEACHER:
                    await self.search_teacher_schedule(user, message)
                else:
                    await self.search_check_group(user, message)
            elif user.subscription_days == const.CHANGES:
                await self.update_subscribe_time(user, message)
            elif user.schedule_day_date == const.CHANGES:
                await self.send_day_schedule_text(user, message)
            elif message == "📅":
                await self.chose_calendar(user)
            elif message == "/debug":
                await self.debug_message(user)
            else:
                await self.send_schedule_menu(user)

    async def send_msg(self, peer_id, message, keyboard=None, dont_parse_links=True):
        try:
            args = {"peer_id": peer_id, "dont_parse_links": int(dont_parse_links)}
            if keyboard is not None:
                args.update({"keyboard": keyboard})
            for message_part in [
                message[i : i + 4000] for i in range(0, len(message), 4000)
            ]:
                await self.vk.messages.send(
                    random_id=get_random_id(), message=message_part, **args
                )
        except Exception as e:
            log.warning(e)
            if e.error_code == 901:
                await self.update_user(
                    peer_id,
                    data=dict(
                        subscription_time=None,
                        subscription_group=None,
                        subscription_days=None,
                    ),
                )

    async def get_short_link(self, url: str):
        url = url.strip()
        if not match(r"(https?://)([\da-z.-]+)\.([a-z.]{2,6})([/\w.-]*)*/?", url):
            return url
        vk_link = match(r"^(https?://)?vk\.com/([\w.-]+)+$", url)
        if vk_link:
            return "@" + vk_link.groups()[1]
        link = await self.vk.utils.getShortLink(url=url)
        return link["short_url"]

    async def vk_bot_answer_unread(self):
        unread = await self.vk.messages.getConversations(filter="unread", count=100)
        log.info("Answering %s unread messages", unread.get("unread_count", 0))
        for conversation in unread["items"]:
            # -- Если вдруг понадобится --
            # payload = json.loads(conversation['last_message']['payload']) if 'payload' in conversation[
            #     'last_message'] else {}
            user_id = conversation["last_message"]["peer_id"]
            try:
                # TODO решить что писать людям
                async with self.db() as conn:
                    user = await (
                        await conn.execute(User.search_user(user_id))
                    ).fetchone()
                    if user is None:
                        await conn.execute(User.add_user(user_id))
                        user = UserProxy(dict(id=user_id))
                    else:
                        user = UserProxy(user)
                # user = User.search_user(user)
                self.loop.create_task(self.send_schedule_menu(user))
            except Exception as e:
                log.warning("Exception in unread: user %s for %s", user_id, e)
                await self.vk.messages.markAsRead(peer_id=user_id)

    """
    Меню расписания
    """

    async def send_schedule_menu(
        self, user: UserProxy, payload: dict = None
    ) -> UserProxy:
        """
        Отправляет меню расписания
        """
        if (
            user.current_name is None
            or user.role is None
            or user.current_name == const.CHANGES
        ):
            await self.send_choice_group(user)
        else:
            await self.send_msg(
                user.id, strings.CHOOSE_MENU, keyboards.schedule_menu(user),
            )
        return user

    async def send_schedule(
        self,
        user: UserProxy,
        start_day: int = 0,
        days: int = 1,
        text: str = "",
        inline_keyboard_date=None,
        payload: dict = None,
    ) -> UserProxy or None:
        """
        Отсылает пользователю расписание

        :param inline_keyboard_date:
        :param text:
        :param start_day: -1 - начало этой недели, -2 - начало следующей
        :param user:
        :param days:
        :param payload:
        :return:
        """
        if payload:
            start_day = payload.get(const.PAYLOAD_START_DAY, 0)
            days = payload.get(const.PAYLOAD_DAYS, 1)
            if payload.get(const.PAYLOAD_SHOW_INLINE_DATE, False):
                inline_keyboard_date = datetime.datetime.strptime(
                    payload[const.PAYLOAD_DATE], const.DATE_FORMAT
                )
                start_day = (
                    inline_keyboard_date
                    - datetime.datetime.today()
                    + datetime.timedelta(days=1)
                ).days
        if start_day == -1 and inline_keyboard_date is None:
            start_day = -datetime.datetime.now().isoweekday() + 1
        elif start_day == -2 and inline_keyboard_date is None:
            start_day = 7 - datetime.datetime.now().isoweekday() + 1
        schedule = await format_schedule(
            user.current_id,
            user.role,
            start_day=start_day,
            days=days,
            text=text,
            show_groups=user.show_groups,
            show_location=user.show_location,
            link_formatter=self.get_short_link,
        )
        if schedule is None:
            log.warning(
                "Error getting schedule: user %s for %s", user.id, user.current_name
            )
            await self.send_msg(user.id, strings.CANT_GET_SCHEDULE)
            return None
        await self.send_msg(
            peer_id=user.id,
            message=schedule,
            keyboard=keyboards.inline_date(inline_keyboard_date)
            if inline_keyboard_date is not None
            else None,
        )
        return user

    async def send_one_day_schedule(
        self, user: UserProxy, payload: dict = None
    ) -> UserProxy:
        """
        Отправляет пользователю предложение написать дату
        """

        await self.update_user(user.id, data=dict(schedule_day_date=const.CHANGES))
        await self.send_msg(
            user.id, strings.WRITE_DATE, keyboards.empty_keyboard(),
        )
        return user

    async def send_day_schedule_text(self, user: UserProxy, date: str) -> UserProxy:
        """
        Отправляет расписание на 1 день

        :param user:
        :param date:
        :return:
        """
        date = date.replace(" ", "")
        await self.update_user(user.id, data=dict(schedule_day_date=None))
        try:
            if len(date.split(".")) == 3:
                date = datetime.datetime.strptime(date, "%d.%m.%Y")
            elif len(date.split(".")) == 2:
                date = datetime.datetime.strptime(
                    f"{date}.{datetime.datetime.now().year}", "%d.%m.%Y"
                )
            else:
                raise ValueError
        except ValueError:
            await self.send_msg(
                user.id, strings.INCORRECT_DATE, keyboards.schedule_menu(user),
            )
            return user
        start_day = (date - datetime.datetime.today() + datetime.timedelta(days=1)).days
        schedule = await format_schedule(
            user.current_id,
            user.role,
            start_day=start_day,
            show_location=user.show_location,
            show_groups=user.show_groups,
            link_formatter=self.get_short_link,
        )
        if schedule is None:
            await self.send_msg(
                user.id,
                strings.CANT_FIND_SCHEDULE_BY_DATE.format(date.strftime("%d.%m.%Y")),
                keyboards.schedule_menu(user),
            )
            return user
        # Сообщение с расписанием и инлайн клавой
        await self.send_msg(user.id, schedule, keyboards.inline_date(date))
        # вернуть клаву расписания
        await self.send_msg(
            user.id, strings.CHOOSE_MENU, keyboards.schedule_menu(user),
        )
        return user

    async def send_choice_group(
        self, user: UserProxy, payload: dict = None
    ) -> UserProxy:
        """
        Отправляет пользователю сообщение о том, что для смены группы трубется ее написать
        """

        await self.update_user(
            user.id, data=dict(current_name=const.CHANGES, role=None)
        )
        return await self.change_role(user)

    async def send_check_group(
        self, user: UserProxy, group_name: str
    ) -> None or UserProxy:
        """
        Проверяет существует ли группа при установке пользователя
        """

        group_name = group_name.strip().replace(" ", "").upper()
        group = await get_group(group_name)
        if group.has_error is False:
            await self.update_user(
                user.id,
                data=dict(
                    current_name=group_name,
                    current_id=group.data,
                    show_location=False,
                    show_groups=False,
                ),
            )
            await self.send_msg(
                user.id,
                strings.GROUP_CHANGED_FOR.format(group_name),
                keyboards.schedule_menu(user),
            )
            return user
        else:
            await self.update_user(user.id, data=dict(current_name=const.CHANGES))
            log.warning("Error setting group: user %s for %s", user.id, group_name)
            if group.error_text == "Timeout error":
                await self.send_msg(
                    user.id, strings.TIMEOUT_ERROR, keyboards.back_to_choosing_role(),
                )
            elif group.error_text == "Not found":
                await self.send_msg(
                    user.id,
                    strings.GROUP_NOT_FOUND.format(group_name),
                    keyboards.back_to_choosing_role(),
                )
                return user
            else:
                log.warning(
                    "Unknown error in setting group: user %s for %s",
                    user.id,
                    group_name,
                )

    async def search_check_group(
        self, user: UserProxy, group_name: str
    ) -> None or UserProxy:
        """
        Проверяет существует ли группа при поиске

        :param user:
        :param group_name:
        :return:
        """

        group_name = group_name.strip().replace(" ", "").upper()
        group = await get_group(group_name)
        if group.has_error is False:
            await self.update_user(
                user.id, data=dict(found_name=group_name, found_id=group.data)
            )
            await self.send_msg(
                user.id,
                strings.GROUP.format(group_name),
                keyboards.find_schedule_menu(user),
            )
            return user
        else:
            await self.update_user(user.id, data=dict(found_name=None))
            log.warning("Error getting schedule: user %s for %s", user.id, group_name)
            if group.error_text == "Timeout error":
                await self.send_msg(
                    user.id, strings.TIMEOUT_ERROR, keyboards.schedule_menu(user),
                )
            elif group.error_text == "Not found":
                await self.send_msg(
                    user.id,
                    strings.GROUP_NOT_FOUND.format(group_name),
                    keyboards.schedule_menu(user),
                )
                return user

    async def send_search(self, user: UserProxy, payload: dict = None) -> UserProxy:
        """
        Поиск преподавателя или группы
        """
        if payload[const.PAYLOAD_ROLE] == const.ROLE_TEACHER:
            found_type = const.ROLE_TEACHER
            message = strings.WRITE_TEACHER
        elif payload[const.PAYLOAD_ROLE] == const.ROLE_STUDENT:
            found_type = const.ROLE_STUDENT
            message = strings.WRITE_GROUP
        else:
            return user
        await self.update_user(
            user.id,
            data=dict(found_id="0", found_name=const.CHANGES, found_type=found_type),
        )
        await self.send_msg(
            user.id, message, keyboards.empty_keyboard(),
        )
        return user

    # LEGACY
    async def send_search_teacher(
        self, user: UserProxy, payload: dict = None
    ) -> UserProxy:
        if payload is None:
            payload = {}
        payload.update({const.PAYLOAD_ROLE: const.ROLE_TEACHER})
        return await self.send_search(user, payload)

    # LEGACY
    async def search_group(self, user, payload: dict = None):
        if payload is None:
            payload = {}
        payload.update({const.PAYLOAD_ROLE: const.ROLE_STUDENT})
        return await self.send_search(user, payload)

    async def search_teacher_schedule(
        self, user: UserProxy, teacher_name: str
    ) -> UserProxy or None:
        """
        Для установки преподавателя через поиск
        Если найден только один преподаватель устанавливает его и отправляет клавиатуру расписания
        Если найдено несколько, то отправляет клавиатуру выбота преподавателя
        """
        await self.send_msg(
            user.id, strings.SEARCHING_FOR_TEACHER,
        )
        teachers = await get_teacher(teacher_name)
        if teachers.has_error:
            log.warning(
                "Error getting schedule: user %s for %s", user.id, user.current_name
            )
            await self.send_msg(
                user.id, strings.TIMEOUT_ERROR, keyboards.schedule_menu(user),
            )
        elif teachers.data:
            teachers = teachers.data
            if len(teachers) == 1:
                await self.update_user(
                    user.id,
                    data=dict(found_id=teachers[0][0], found_name=teachers[0][1]),
                )
                await self.send_msg(
                    user.id,
                    strings.FOUND_TEACHER.format(teachers[0][1])
                    + "\n"
                    + strings.CHOOSE_TIMEDELTA,
                    keyboards.find_schedule_menu(user),
                )
            else:
                await self.send_msg(
                    user.id,
                    strings.CHOOSE_CURRENT_TEACHER,
                    keyboards.found_list(teachers),
                )
                return user
        else:
            await self.update_user(
                user.id, data=dict(found_id=None, found_name=None, found_type=None)
            )
            await self.send_msg(
                user.id, strings.TEACHER_NOT_FOUND, keyboards.schedule_menu(user),
            )
            return None

    async def search_teacher_to_set(
        self, user: UserProxy, teacher_name: str
    ) -> UserProxy or None:
        # self.vk.messages.send(
        #     peer_id=user.id,
        #     random_id=get_random_id(),
        #     message=strings.SEARCHING,
        # )
        teachers = await get_teacher(teacher_name)
        if teachers.has_error:
            log.warning("Error getting schedule: user %s for %s", user.id, teacher_name)
            await self.send_msg(
                user.id, strings.TIMEOUT_ERROR, keyboards.back_to_choosing_role(),
            )
        elif teachers.data:
            teachers = teachers.data
            if len(teachers) == 1:
                await self.update_user(
                    user.id,
                    data=dict(
                        current_id=teachers[0][0],
                        current_name=teachers[0][1],
                        show_location=True,
                        show_groups=True,
                    ),
                )
                await self.send_msg(
                    user.id,
                    strings.FOUND_TEACHER.format(teachers[0][1]),
                    keyboards.schedule_menu(user),
                )
            else:
                await self.send_msg(
                    user.id,
                    strings.CHOOSE_CURRENT_TEACHER,
                    keyboards.found_list(teachers, to_set=True),
                )
                return user
        else:
            # User.update_user(user=user, data=dict(current_id=))
            await self.send_msg(
                user.id, strings.TEACHER_NOT_FOUND, keyboards.back_to_choosing_role(),
            )
            return None

    async def set_teacher(self, user, payload: dict = None):
        await self.update_user(
            user.id,
            data=dict(
                current_id=payload[const.PAYLOAD_FOUND_ID],
                current_name=payload[const.PAYLOAD_FOUND_NAME],
                show_location=True,
                show_groups=True,
            ),
        )
        await self.send_msg(
            user.id,
            strings.FOUND_TEACHER.format(payload[const.PAYLOAD_FOUND_NAME]),
            keyboards.schedule_menu(user),
        )

    async def send_teacher(self, user, payload: dict = None):
        """
        Отправляет меню с выбором промежутка расписания для пользователя
        """
        if const.PAYLOAD_FOUND_ID in payload and const.PAYLOAD_FOUND_NAME in payload:
            await self.update_user(
                user.id,
                data=dict(
                    found_id=payload[const.PAYLOAD_FOUND_ID],
                    found_name=payload[const.PAYLOAD_FOUND_NAME],
                ),
            )
            await self.send_msg(
                user.id, strings.CHOOSE_TIMEDELTA, keyboards.find_schedule_menu(user),
            )
        else:
            await self.update_user(
                user.id, data=dict(found_id=None, found_name=None, found_type=None)
            )
            await self.send_msg(
                user.id, strings.CANT_FIND_USER, keyboards.schedule_menu(user)
            )

    # TODO Менять на send_search_schedule
    async def send_teacher_schedule(
        self, user: UserProxy, start_day: int = 0, days: int = 1, payload: dict = None
    ) -> UserProxy or None:
        """
        Отсылает пользователю расписание преподавателя
        """
        start_day = payload.get(const.PAYLOAD_START_DAY, 0)
        days = payload.get(const.PAYLOAD_DAYS, 1)
        if start_day == -1:
            start_day = -datetime.datetime.now().isoweekday() + 1
        elif start_day == -2:
            start_day = 7 - datetime.datetime.now().isoweekday() + 1
        schedule = await format_schedule(
            user.found_id,
            type=user.found_type,
            start_day=start_day,
            days=days,
            show_groups=True,
            show_location=True,
            link_formatter=self.get_short_link,
        )
        await self.update_user(
            user.id, data=dict(found_id=None, found_name=None, found_type=None)
        )
        await self.send_msg(
            user.id,
            schedule or strings.CANT_GET_SCHEDULE,
            keyboards.schedule_menu(user),
        )
        return user

    """
    Меню настроек
    """

    async def show_groups_or_location(
        self, user: UserProxy, payload: dict
    ) -> UserProxy:
        """
        Обновляет поля
        """
        act_type = payload[const.PAYLOAD_TYPE]

        if act_type == const.SETTINGS_TYPE_GROUPS:
            if not user.show_groups:
                await self.update_user(user.id, data=dict(show_groups=True))
                user.upd("show_groups", True)
                message = "Список групп будет отображаться в раписании"
            else:
                await self.update_user(user.id, data=dict(show_groups=False))
                user.upd("show_groups", False)
                message = "Список групп не будет отображаться в раписании"
        elif act_type == const.SETTINGS_TYPE_LOCATION:
            if not user.show_location:
                await self.update_user(user.id, data=dict(show_location=True))
                user.upd("show_location", True)
                message = "Список корпусов будет отображаться в раписании"
            else:
                await self.update_user(user.id, data=dict(show_location=False))
                user.upd("show_location", False)
                message = "Список корпусов не будет отображаться в раписании"
        else:
            message = strings.ERROR
        await self.send_msg(user.id, message, keyboards.settings_menu(user))
        return user

    async def send_settings_menu(
        self, user: UserProxy, payload: dict = None
    ) -> UserProxy:
        """
        Отправляет меню настроек
        """

        await self.send_msg(user.id, strings.WHAT_TO_SET, keyboards.settings_menu(user))
        return user

    """
    Подписка на расписание
    """

    async def unsubscribe_schedule(
        self, user: UserProxy, payload: dict = None
    ) -> UserProxy:
        """
        Отправляет время для подписки на расписание
        """

        await self.update_user(
            user.id,
            data=dict(
                subscription_time=None, subscription_group=None, subscription_days=None
            ),
        )
        await self.send_msg(
            user.id,
            "Вы отписались от рассылки расписания",
            keyboards.schedule_menu(user),
        )
        return user

    async def subscribe_schedule(
        self, user: UserProxy, payload: dict = None
    ) -> UserProxy:
        """
        Отправляет время для подписки на расписание
        """

        await self.update_user(
            user.id,
            data=dict(
                subscription_time=const.CHANGES,
                subscription_group=const.CHANGES,
                subscription_days=const.CHANGES,
            ),
        )
        await self.send_msg(
            user.id,
            "Напишите или выберите время в которое хотите получать раписание\n\nНапример: «12:35»",
            keyboards.subscribe_to_schedule_start_menu(user),
        )
        return user

    async def update_subscribe_time(
        self, user: UserProxy, time: str
    ) -> UserProxy or None:
        """
        Обновляет время для подписки на расписание
        """
        try:
            time = datetime.datetime.strptime(time, "%H:%M").strftime("%H:%M")
        except ValueError:
            await self.update_user(
                user.id,
                data=dict(
                    subscription_days=None,
                    subscription_time=None,
                    subscription_group=None,
                ),
            )
            await self.send_msg(
                user.id, strings.INCORRECT_DATE_FORMAT, keyboards.schedule_menu(user)
            )
            return user
        await self.update_user(
            user.id,
            data=dict(subscription_time=time, subscription_group=user.current_name),
        )
        schedule_for = "группы" if user.role == "student" else "преподавателя"
        await self.send_msg(
            user.id,
            f"Формируем расписания для {schedule_for} {user.current_name} в {time}\nВыберите период, на который вы "
            f"хотите получать расписание",
            keyboards.subscribe_to_schedule_day_menu(user),
        )
        return user

    async def update_subscribe_day(
        self, user: UserProxy, payload: dict
    ) -> UserProxy or None:
        """
        Отправляет день для подписки на расписание
        """
        menu = payload[const.PAYLOAD_TYPE]

        if menu == const.SUBSCRIPTION_TODAY:
            subscription_days = const.SUBSCRIPTION_TODAY
            day = "сегодня"
        elif menu == const.SUBSCRIPTION_TOMORROW:
            subscription_days = const.SUBSCRIPTION_TOMORROW
            day = "завтра"
        elif menu == const.SUBSCRIPTION_TODAY_TOMORROW:
            subscription_days = const.SUBSCRIPTION_TODAY_TOMORROW
            day = "текущий и следующий день"
        elif menu == const.SUBSCRIPTION_WEEK:
            subscription_days = const.SUBSCRIPTION_WEEK
            day = "текущую неделю"
        elif menu == const.SUBSCRIPTION_NEXT_WEEK:
            subscription_days = const.SUBSCRIPTION_NEXT_WEEK
            day = "следующую неделю"
        else:
            await self.update_user(
                user.id,
                data=dict(
                    subscription_time=None,
                    subscription_group=None,
                    subscription_days=None,
                ),
            )
            await self.send_msg(
                user.id,
                f"Не удалось добавить в рассылку расписания",
                keyboards.schedule_menu(user),
            )
            return None
        await self.update_user(user.id, data=dict(subscription_days=subscription_days))
        schedule_for = "группы" if user.role == "student" else "преподавателя"
        await self.send_msg(
            user.id,
            f"Вы подписались на раписание {schedule_for} {user.subscription_group}\nТеперь каждый день в "
            f"{user.subscription_time} вы будете получать расписание на {day}",
            keyboards.schedule_menu(user),
        )
        return user

    async def chose_calendar(self, user: UserProxy, payload: dict = None):
        await self.send_msg(user.id, "(¬‿¬)")
        return user

    async def calendar_link(self, user: UserProxy, **kwargs):
        query = urlencode(
            {
                "name": user.current_name,
                "type": "group" if user.role == "student" else "person",
                "id": user.current_id,
            }
        )
        log.info("%s asked for calendar for group %s", user.id, user.current_name)
        await self.send_msg(
            user.id, f"https://schedule.fa.ru/?{query}", dont_parse_links=False
        )

    async def change_role(self, user: UserProxy, payload: dict = None):
        await self.send_msg(user.id, strings.WELCOME, keyboards.choose_role())
        return user

    async def set_role(self, user: UserProxy, payload: dict):
        role = payload[const.PAYLOAD_ROLE]
        message = (
            strings.GROUP_EXAMPLE
            if role == const.ROLE_STUDENT
            else strings.TEACHER_EXAMPLE
        )
        await self.send_msg(user.id, message, keyboards.back_to_choosing_role())
        await self.update_user(
            user.id, data=dict(current_name=const.CHANGES, role=role)
        )
        return user

    async def search(self, user, payload: dict = None):
        await self.send_msg(user.id, strings.WHAT_TO_FIND, keyboards.search_menu())
        return user

    async def cancel(self, user, payload: dict = None):
        cancel_changes = User.cancel_changes(user.id, user)
        if cancel_changes is not None:
            async with self.db() as conn:
                await conn.execute(cancel_changes)
        await self.send_schedule_menu(user, payload)

    async def debug_message(self, user, payload: dict = None):
        await self.send_msg(user.id, str(user))

    async def user_update_2(self, user):
        log.warning("Renew user %s group %s", user.id, user.current_name)
        await self.update_user(
            user.id, data=dict(current_name=None, role=None, update="3")
        )
        user.current_name = None
        await self.send_schedule_menu(user)
