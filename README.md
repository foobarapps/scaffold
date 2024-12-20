# Scaffold

Scaffold is a full-stack web framework that makes it easy to build apps following the Domain-Driven Design (DDD) approach and layered/onion architecture (following the dependency inversion principle). It's built on top of Quart (async Flask) and SQLAlchemy and comes with the following features for building modern web apps out of the box:

- Async-first
- Controller-based web framework built on top of Quart (so it's very familiar if you know Flask/Quart) and Jinja2 for templating
- WebSockets support
- Generic UoW and repository implementation on top of SQLAlchemy and database migrations with Alembic
- Easy-to-use DI container with auto-wiring based on type annotations
- Async task manager
- Task queue built on top of Postgres
- Scheduled/Cron jobs
- Pub/sub built on top of Postgres
- Sending emails
- Static code checking using Ruff, mypy, and Import Linter in pre-commit hooks and a CI/CD pipeline
- CLI app framework
- WTForms integration with CSRF protection
- Asset pipeline
- Dev web server with support for debugging from your favorite IDE
- Local development with Docker Compose (for dev/prod parity)
- Zero-downtime deployment with Docker Swarm, Traefik (with Let's Encrypt certificates), and GitHub Actions
- Project generation via cruft (which builds on top of Cookiecutter) that makes it easy to update the project to the latest template version
- Dependency management via uv
- And more...

It consists of a library (this repo) and a [project template](https://github.com/foobarapps/scaffold-template).

## Architecture and design principles

Applications built with Scaffold follow the Domain-Driven Design (DDD) approach and are organized into the following layers:

- Domain layer
- Application layer
- Presentation layer
- Infrastructure layer

![layers](docs/assets/domain-driven-design-layers.png)
*Source: https://abp.io/docs/4.2/Domain-Driven-Design-Implementation-Guide*

Each layer makes use of building blocks based on design patterns for common problems, which are described in the following sections.

Having the code organized in layers has the following advantages:

- Separation of Concerns: Each layer has a specific responsibility and purpose which makes it easier to reason about the code and it makes it easier to know where to put what.
- Maintainability: When components are properly separated into layers, you can modify one layer without affecting others as long as you maintain the interfaces between them. For instance, you could completely replace your database technology while keeping the business logic intact.
- Testability: Layers can be tested in isolation. For example, the application layer can be tested without a UI or database. This enables more focused and reliable testing.
- Flexibility and Reusability: Common functionality can be reused across different parts of the application. For example, a well-designed application layer could potentially serve multiple front-ends (e.g. web and CLI) without modifications.

### Domain layer

In the domain layer, we focus on modeling the business and its rules without being distracted by technical concerns and frameworks. This is where the main business value of the software lies. It's written in plain Python objects and it does not depend on any other layers.

The main building blocks of the domain layer are Entities, Value Objects, and Services which are used to express a rich object model of the business domain (domain model). All class and method names should reflect the **Ubiquitous** Language used by domain experts.

**Entities** are objects that have a distinct identity that runs through time and different states (lifecycle). They are defined by their identity rather than their attributes (which might change over time). Examples include a Customer or an Order, which are tracked by unique identifiers.

**Value Objects** are objects that are defined by their attributes (which describe some characteristics of a thing) and do not have a conceptual identity. They are immutable and interchangeable when they have the same attribute values. Examples include a Money amount, a Date, or a Color.

Both Entities and Value Objects contain both data and behavior and are clustered into **Aggregates** based on their transactional consistency boundaries.

Stateless **Sevices** are used to perform business operations that don’t fit as an operation on an Entity or a Value Object and may involve multiple domain objects.

All Entities / Aggregates are persisted and later retrieved using **Repositories**, which provide a collection-like abstraction over persistence mechanisms such as file systems or SQL databases. In the domain layer, we define only the interface and implement it in the infra layer. This way, we can for example provide an in-memory implementation for running unit tests without a dependence on an external database.

Finally, **Modules** (Python packages) are used to organize domain objects into cohesive sets based on business/domain concepts (not technical concepts).

### Application layer

The main building block of the application layer is **Application Services** which implement application use cases by orchestrating domain objects to accomplish a given use case using the domain model and assembles the results using **Assemblers** to **Data Transfer Objects (DTOs)** which are designed to hold the entire number of attributes that need to be displayed in a view and are then passed to the Presentation Layer.

Application Services and DTOs serve as the interface between the Application and Presentation layer. This way we can be sure that any changes to the Application or Domain Layer do not impact the presentation layer unless we change the interface. So let's say we make some changes to the domain model, then we only need to update the corresponding assemblers (which map domain objects to DTOs) and nothing else.

Another building block is the **Unit of Work** object/pattern, which is an abstraction that groups a set of operations into a single unit (e.g. database transaction).

In the Application layer, we also define interfaces for services that are required by the application layer but implemented in the infra layer (e.g. sending emails).

### Presentation layer

The presentation layer is used to communicate with the outside world (end-users). It can have different endpoints such as a web app (HTML/HTTP), CLI (used manually or via scheduled tasks), worker (which accepts tasks via a task queue), etc.

In this layer, we typically use technical frameworks such as Flask/Quart for web applications or Click for CLI applications to parse the user input, map/translate it to DTOs, and use Application Services to run a requested business operation.

It should be relatively thin and be responsible only for parsing the user's input and serializing the response. There should be no business or application logic.

### Infrastructure layer

The infrastructure layer has a supporting role for the other layers and that's where the technical concerns, such as persistence, messaging, sending emails, or talking to external APIs, are implemented.

Instead of all the layers depending on the infrastructure, the dependency is reversed following the dependency inversion principle. So instead of the domain and application layers depending on infrastructure implementations, the infra layer depends on abstractions in the other layers and implements them. This inversion ensures that business logic remains pure and independent of technical details, while the infrastructure layer adapts to meet the interfaces required by the domain.

At runtime, the right implementations are provided via (constructor) dependency injection. Scaffold provides a simple dependency injection container/framework that can inject the right dependencies based on type annotations. The advantage of this approach is that different entry points can depend on different implementations. So for example, when testing the application layer, you can provide in-memory repositories for faster test suites instead of database-backed repositories.

## Tutorial

In this tutorial, we are going to build a simple chatbot. While basic, the app goes beyond a hello-world example and showcases most of the Scaffold's functionality. When users first visit the website, it asks them for their email address to sign them in. It then sends them a welcome email asynchronously, independent of the HTTP request flow. After that, the user can start a chat with the chatbot. Replying to users' messages is done in a background task that runs in a different process (for better reliability and scalability) via the ChatGPT API. When it receives the reply from the API, it notifies the app via a pub/sub mechanism and sends the reply to the user via a WebSocket connection. On the front end, it's using HTMX to handle the WebSockets and vanilla CSS with BEM components for styling.

![chatbot-demo](docs/assets/chatbot-demo.gif)

> [!WARNING]
> This tutorial is still a work in progress so for now I just copy pasted all the code I wrote for the app here so that you can go through it on your own and see how it was implemented. Later, I will describe step-by-step how to write the whole app from scratch.

### Getting started

```shell
cruft create https://github.com/foobarapps/scaffold-template
git init
git commit -m "Initial commit"
```

### Domain layer

`app/domain/message.py`:
```python
import abc
import dataclasses
import datetime
from collections.abc import Sequence
from typing import override

from .base import Entity, EntityId


@dataclasses.dataclass(frozen=True)
class MessageId(EntityId):
    pass


@dataclasses.dataclass(frozen=True)
class UserId(EntityId):
    pass


class Message(Entity):
    def __init__(
        self,
        id: MessageId,
        user_id: UserId,
        content: str,
        sent_at: datetime.datetime,
    ) -> None:
        self._id = id
        self._user_id = user_id
        self._content = content
        self._sent_at = sent_at

    @property
    @override
    def id(self) -> MessageId:
        return self._id

    @property
    def user_id(self) -> UserId:
        return self._user_id

    @property
    def content(self) -> str:
        return self._content

    @property
    def sent_at(self) -> datetime.datetime:
        return self._sent_at


class UserMessage(Message):
    pass


class BotMessage(Message):
    pass


class MessageRepository(abc.ABC):
    @abc.abstractmethod
    async def get(self, id: MessageId) -> Message | None:
        pass

    @abc.abstractmethod
    def add(self, message: Message) -> None:
        pass

    @abc.abstractmethod
    async def get_user_messages(self, user_id: UserId) -> Sequence[Message]:
        pass
```

`app/domain/chatbot.py`:
```python
import abc
from collections.abc import Sequence

from .message import BotMessage, Message


class Chatbot(abc.ABC):
    @abc.abstractmethod
    async def reply_to_conversation(self, conversation: Sequence[Message]) -> BotMessage:
        pass
```

### Application layer

#### Application services

`app/application/services/user_service.py`:
```python
from app.application.interfaces import (
    NotificationService,
    TaskManager,
)


class UserService:
    def __init__(
        self,
        notification_service: NotificationService,
        task_manager: TaskManager,
    ) -> None:
        self.notification_service = notification_service
        self.task_manager = task_manager

    async def send_welcome_message(self, email: str) -> None:
        self.task_manager.run_task(self.notification_service.send_welcome_message, email)
```

`app/application/services/message_service.py`:
```python
import dataclasses
import datetime
import uuid
from collections.abc import AsyncGenerator, Sequence

from app.application.assemblers.message_assembler import MessageAssembler
from app.application.backgroundtasks.tasks.reply_to_conversation_task import (
    ReplyToConversationTask,
)
from app.application.dtos.message import Message as MessageDTO
from app.application.interfaces.pub_sub_service import PubSubService
from app.application.interfaces.task_queue import TaskQueue
from app.application.interfaces.uow import UnitOfWorkFactory
from app.domain.chatbot import Chatbot
from app.domain.message import MessageId, UserId, UserMessage


@dataclasses.dataclass
class MessageDoesNotExistError(Exception):
    message_id: str


class MessageService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        chatbot: Chatbot,
        task_queue: TaskQueue,
        pub_sub: PubSubService,
    ) -> None:
        self.unit_of_work_factory = unit_of_work_factory
        self.chatbot = chatbot
        self.task_queue = task_queue
        self.pub_sub = pub_sub

    async def get_messages_for_conversation(self, user_id: uuid.UUID) -> Sequence[MessageDTO]:
        uow = self.unit_of_work_factory.create()
        async with uow:
            messages = await uow.messages.get_user_messages(UserId(user_id))
            return [MessageAssembler().assemble_dto(message) for message in messages]

    async def send_user_message(
        self,
        user_id: uuid.UUID,
        message_id: uuid.UUID,
        content: str,
        sent_at: datetime.datetime,
    ) -> None:
        uow = self.unit_of_work_factory.create()
        async with uow:
            message = UserMessage(
                id=MessageId(message_id),
                user_id=UserId(user_id),
                content=content,
                sent_at=sent_at,
            )
            uow.messages.add(message)
            await uow.commit()

            await self.task_queue.enqueue(ReplyToConversationTask(user_id))

            await self.pub_sub.publish(self.get_channel_name(user_id), str(message_id))

    async def reply_to_user_latest_message(self, user_id: uuid.UUID) -> None:
        uow = self.unit_of_work_factory.create()
        async with uow:
            messages = await uow.messages.get_user_messages(UserId(user_id))

            reply = await self.chatbot.reply_to_conversation(messages)

            uow.messages.add(reply)
            await uow.commit()

            await self.pub_sub.publish(self.get_channel_name(user_id), str(reply.id.value))

    async def subscribe_to_user_messages(self, user_id: uuid.UUID) -> AsyncGenerator[MessageDTO]:
        uow = self.unit_of_work_factory.create()
        async for message_id_str in self.pub_sub.subscribe(self.get_channel_name(user_id)):
            async with uow:
                message = await uow.messages.get(MessageId(uuid.UUID(message_id_str)))

                if not message:
                    raise MessageDoesNotExistError(message_id_str)

                message_dto = MessageAssembler().assemble_dto(message)

                yield message_dto

    @staticmethod
    def get_channel_name(user_id: uuid.UUID) -> str:
        return f"user:{user_id}:messages"
```

#### Background tasks

`app/application/backgroundtasks/tasks/reply_to_conversation_task.py`:
```python
import dataclasses
import uuid

from app.application.interfaces.task_queue import Task


@dataclasses.dataclass(frozen=True)
class ReplyToConversationTask(Task):
    user_id: uuid.UUID
```

`app/application/backgroundtasks/handlers/reply_to_conversation_task_handler.py`:
```python
from typing import override

from app.application.services.message_service import MessageService

from ..tasks.reply_to_conversation_task import ReplyToConversationTask
from .base import GenericBaseTaskHandler


class ReplyToConversationTaskHandler(GenericBaseTaskHandler[ReplyToConversationTask]):
    def __init__(
        self,
        message_service: MessageService,
    ) -> None:
        self.message_service = message_service

    @override
    async def handle(self, task: ReplyToConversationTask) -> None:
        await self.message_service.reply_to_user_latest_message(task.user_id)
```

#### Interfaces

`app/application/interfaces/notification_service.py`:
```python
import abc


class NotificationService(abc.ABC):
    @abc.abstractmethod
    async def send_welcome_message(self, email: str) -> None: ...
```

`app/application/interfaces/uow.py`
```python
# ...

from app.domain.message import MessageRepository


class UnitOfWork(abc.ABC):
    messages: MessageRepository
    
    # ...
```

#### DTOs

`app/application/dtos/message.py`:
```python
import dataclasses
import datetime


@dataclasses.dataclass(frozen=True)
class Message:
    content: str
    sent_at: datetime.datetime
    is_from_bot: bool
```

#### Assemblers

`app/application/assemblers/message_assembler.py`:
```python
from app.application.dtos.message import Message as MessageDTO
from app.domain.message import BotMessage, Message


class MessageAssembler:
    @staticmethod
    def assemble_dto(message: Message) -> MessageDTO:
        return MessageDTO(
            content=message.content,
            sent_at=message.sent_at,
            is_from_bot=isinstance(message, BotMessage),
        )
```

### Infrastructure layer

`app/infrastructure/persistence_model.py`:
```python
import datetime
import uuid

from scaffold.persistence.model import Base, EntityMixin, TimestampMixin
from sqlalchemy.orm import Mapped, mapped_column


class Message(EntityMixin, TimestampMixin, Base):
    __tablename__ = "message"

    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    content: Mapped[str]
    sent_at: Mapped[datetime.datetime] = mapped_column(nullable=False)
    is_from_bot: Mapped[bool] = mapped_column(nullable=False)
```

`app/infrastructure/repositories/sql_message_repository.py`:
```python
from collections.abc import Sequence
from typing import override

import sqlalchemy as sa
from scaffold.persistence.repository import GenericSqlRepository

from app.domain.message import (
    BotMessage,
    Message,
    MessageId,
    MessageRepository,
    UserId,
    UserMessage,
)
from app.infrastructure.persistence_model import Message as MessageDTO


class SqlMessageRepository(GenericSqlRepository[Message, MessageId, MessageDTO], MessageRepository):
    @override
    async def get_user_messages(self, user_id: UserId) -> Sequence[Message]:
        dtos = (
            await self._session.scalars(
                sa.select(MessageDTO).where(MessageDTO.user_id == user_id.value).order_by(MessageDTO.sent_at.desc()),
            )
        ).all()
        return [self.map_dto_to_entity_and_track(dto) for dto in dtos]

    @override
    def _map_entity_to_dto(self, entity: Message) -> MessageDTO:
        return MessageDTO(
            id=entity.id.value,
            user_id=entity.user_id.value,
            content=entity.content,
            sent_at=entity.sent_at,
            is_from_bot=isinstance(entity, BotMessage),
        )

    @override
    def _map_dto_to_entity(self, dto: MessageDTO) -> Message:
        if dto.is_from_bot:
            return BotMessage(
                id=MessageId(dto.id),
                user_id=UserId(dto.user_id),
                content=dto.content,
                sent_at=dto.sent_at,
            )
        return UserMessage(
            id=MessageId(dto.id),
            user_id=UserId(dto.user_id),
            content=dto.content,
            sent_at=dto.sent_at,
        )
```

`app/infrastructure/uow.py`:
```python
# ...
from app.infrastructure.repositories.sql_message_repository import SqlMessageRepository


class SqlUnitOfWork(GenericSqlUnitOfWork, UnitOfWork):
    @typing.override
    def __init__(self, session: AsyncSession) -> None:
        # ...
        self.messages = SqlMessageRepository(session)
```

`app/infrastructure/services/chatbot.py`:
```python
import datetime
from collections.abc import Sequence
from typing import override

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from scaffold.uuid7 import uuid7

from app.domain.chatbot import Chatbot
from app.domain.message import BotMessage, Message, MessageId, UserMessage


class OpenAIBot(Chatbot):
    def __init__(self, client: AsyncOpenAI) -> None:
        self.client = client

    @override
    async def reply_to_conversation(self, conversation: Sequence[Message]) -> BotMessage:
        latest_message = conversation[0]

        completion_messages: list[ChatCompletionMessageParam] = []
        for message in reversed(conversation):
            if isinstance(message, UserMessage):
                completion_messages.append({"role": "user", "content": message.content})
            else:
                completion_messages.append({"role": "assistant", "content": message.content})

        completion = await self.client.chat.completions.create(
            model="gpt-4o",
            messages=completion_messages,
        )

        return BotMessage(
            id=MessageId(uuid7()),
            user_id=latest_message.user_id,
            content=completion.choices[0].message.content or "",
            sent_at=datetime.datetime.now(),
        )
```

`app/infrastructure/services/emailnotificationservice/__init__.py`:
```python
from typing import override

from scaffold.email_notification_service import (
    EmailNotificationService as BaseEmailNotificationService,
)
from scaffold.email_notification_service import Message

from app.application.interfaces.notification_service import NotificationService


class EmailNotificationService(BaseEmailNotificationService, NotificationService):
    @override
    async def send_welcome_message(self, email: str) -> None:
        text = await self.render_template("text/welcome.txt")

        message = Message(
            subject="Welcome!",
            recipients=[email],
            sender=self.default_sender_email,
            body=text,
        )
        await self.mail_sender.send(message)
```

```shell
alembic revision --autogenerate -m "Add message table"
```

```shell
alembic upgrade head
```

### Presentation layer

`app/presentation/web/controllers/home_controller.py`:
```python
import uuid

from quart import ResponseReturnValue
from scaffold.web import BaseController, controller, route

from app.application.services.user_service import UserService
from app.presentation.web.forms import SignUpForm


@controller(name="home")
class HomeController(BaseController):
    def __init__(
        self,
        user_service: UserService,
    ) -> None:
        self.user_service = user_service

    @route("/")
    async def index(self) -> ResponseReturnValue:
        if "user_id" in self.session:
            return self.redirect(self.url_for("messages.index"))

        form = SignUpForm()
        return await self.render_template("home/index.html", form=form)

    @route("/", methods=["POST"])
    async def create(self) -> ResponseReturnValue:
        form = SignUpForm(await self.request.form)

        if form.validate():
            user_id = uuid.uuid4()
            await self.user_service.send_welcome_message(email=form.email.data)
            self.session["user_id"] = user_id
            return self.redirect(self.url_for(".index"))

        return await self.render_template("home/index.html", form=form)
```

`app/presentation/web/forms.py`:
```python
from scaffold.web.forms import BaseForm
from wtforms import EmailField, validators


class SignUpForm(BaseForm):
    email = EmailField("Email address", [validators.InputRequired()])
```

`app/presentation/web/templates/home/index.html`:
```html
{% extends "layout.html" %}
{% block content %}
  <div class="start-chat">
    <h1 class="start-chat__title">Start a chat</h1>
    <form class="start-chat__form"
          method="post"
          action="{{ url_for('.create') }}">
      {{ form.email(class="start-chat__input", placeholder="Enter your email") }}
      {{ form.csrf_token }}
      <button type="submit" class="start-chat__submit">Start Chat</button>
    </form>
  </div>
{% endblock content %}
```

`app/presentation/web/controllers/messages_controller.py`:
```python
import asyncio
import datetime
import json
import uuid

from quart import ResponseReturnValue
from scaffold.uuid7 import uuid7
from scaffold.web import (
    BaseController,
    controller,
    error_handler,
    login_required,
    route,
)
from werkzeug.exceptions import Unauthorized

from app.application.services.message_service import MessageService


@controller(name="messages", url_prefix="/messages")
class MessagesController(BaseController):
    def __init__(
        self,
        message_service: MessageService,
    ) -> None:
        self.message_service = message_service

    @error_handler(Unauthorized)
    def handle_unauthorized(self, exception: Unauthorized) -> ResponseReturnValue:
        return self.redirect(self.url_for("home.index"))

    @route("/")
    @login_required
    async def index(self) -> ResponseReturnValue:
        user_id = self.session["user_id"]
        messages = await self.message_service.get_messages_for_conversation(user_id)
        return await self.render_template("messages/index.html", messages=messages)

    @route("/", websocket=True)
    @login_required
    async def websockets(self) -> None:
        user_id = self.session["user_id"]
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.receive_websocket_messages(user_id))
                tg.create_task(self.send_websocket_messages(user_id))

        except asyncio.CancelledError:
            # Handle disconnection here
            raise

    async def receive_websocket_messages(
        self,
        user_id: uuid.UUID,
    ) -> None:
        while True:
            data = json.loads(await self.websocket.receive())

            await self.message_service.send_user_message(
                user_id=user_id,
                message_id=uuid7(),
                content=data["content"],
                sent_at=datetime.datetime.now(),
            )

    async def send_websocket_messages(
        self,
        user_id: uuid.UUID,
    ) -> None:
        async for message in self.message_service.subscribe_to_user_messages(user_id=user_id):
            response = await self.render_template(
                "messages/partials/conversation.html",
                messages=[message],
            )
            await self.websocket.send(response)
```

`app/presentation/web/templates/messages/index.html`:
```html
{% extends "layout.html" %}
{% block content %}
  <div class="chat" hx-ext="ws" ws-connect="{{ url_for('.websockets') }}">
    {% include "messages/partials/conversation.html" %}
  </div>
{% endblock content %}
```

`app/presentation/web/templates/messages/partials/conversation.html`:
```html
<div class="chat__messages" id="chat-messages" hx-swap-oob="afterbegin">
  {% for message in messages %}
    <div class="message {{ "message--sent" if not message.is_from_bot else "message--received" }}">
      <div class="message__content">
        <div class="message__bubble">{{ message.content }}</div>
        <div class="message__time">{{ message.sent_at | time_ago }}</div>
      </div>
    </div>
  {% endfor %}
</div>
<form class="chat__form" id="chat-form" ws-send hx-swap-oob="true">
  <div class="chat__input-container">
    <input autofocus
           name="content"
           class="chat__input"
           type="text"
           placeholder="Type your message...">
    <button type="submit" class="chat__submit">Send</button>
  </div>
</form>
```

### Bootstrap

`app/bootstrap.py`:
```python
# ...
from openai import AsyncOpenAI
from app.infrastructure.services.chatbot import OpenAIBot

def bootstrap() -> Container:
    container = Container()
    
    # ...
    
    container.add_transient(Chatbot, lambda _: OpenAIBot(client=AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])))
    
    # ...
    
    return container
```

### Running the app

```shell
docker compose up
```

Now the web app should be accessible at `app.localhost`.