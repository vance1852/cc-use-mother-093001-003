"""技能赛训协作基础服务的服务端基础包。"""

from .review_service import BlindReviewService
from .service import DomainService

__all__ = ["DomainService", "BlindReviewService"]
