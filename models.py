from pydantic import BaseModel, computed_field
from datetime import datetime


class TikTokVideo(BaseModel):
    video_id: str
    author: str
    author_sec_uid: str = ""
    description: str = ""
    hashtags: list[str] = []
    views: int = 0
    likes: int = 0
    shares: int = 0
    comments: int = 0
    saves: int = 0
    duration: float = 0.0
    play_url: str = ""
    download_url: str = ""
    cover_url: str = ""
    sound_name: str = ""
    created_at: datetime | None = None
    content_format: str = "video"  # video | carousel | duet
    main_format: str = "ugc"  # ugc | carousel
    image_count: int = 0
    image_urls: list[str] = []

    @computed_field
    @property
    def engagement_rate(self) -> float:
        if self.views == 0:
            return 0.0
        return round(
            (self.likes + self.comments + self.shares + self.saves) / self.views * 100,
            4,
        )


class TikTokProfile(BaseModel):
    sec_uid: str
    unique_id: str
    nickname: str = ""
    followers: int = 0
    following: int = 0
    total_likes: int = 0
    video_count: int = 0
    avatar_url: str = ""


class SearchResult(BaseModel):
    videos: list[TikTokVideo] = []
    profiles: list[TikTokProfile] = []
    has_more: bool = False
    cursor: str = ""
