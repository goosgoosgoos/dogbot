import slixmpp

from .core import DogCoreMixin
from .commands import DogCommandsMixin
from .muc import DogMucMixin
from .llm import DogLlmMixin
from .router import DogRouterMixin
from .privacy import PrivacyMixin
from .hunt import HuntMixin
from .nickname import NicknameMixin
from .moderation import DogModerationMixin


class DogBot(DogCoreMixin, DogCommandsMixin, DogMucMixin, DogLlmMixin, DogRouterMixin, PrivacyMixin, HuntMixin, NicknameMixin, DogModerationMixin, slixmpp.ClientXMPP):
    pass
