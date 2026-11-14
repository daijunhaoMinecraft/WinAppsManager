from collections.abc import Iterable
from pathlib import Path
from typing import Literal

if __package__:
    from .models import AppDefinition, CatalogData, Release
else:
    from models import AppDefinition, CatalogData, Release


class CatalogRepository:
    """Catalog loader with an optional app allow-list.

    ``data_file`` holds the curated entries. Extra entries live in the sibling ``apps.d``
    directory (one JSON file per category group) and are merged after it; every file uses
    the same schema, and a duplicate app id across files is a startup error. The extra
    directory is derived from ``data_file`` so a test fixture directory never picks up the
    production entries of another directory.

    ``allowed_app_ids`` 是可选白名单（内置精选源的启用范围）：``None`` 表示不限制，
    否则 ``list_apps`` / ``get_app`` 只暴露白名单内的软件。完整目录仍会加载、做跨文件
    重复 id 与图标校验，白名单外的条目保留在 :attr:`all_apps` 里（不会删除任何目录文件）；
    配置里出现目录中不存在的 id 会在启动时抛 ``ValueError``。
    """

    def __init__(
        self,
        data_file: Path,
        icon_directory: Path,
        extra_directory: Path | None = None,
        allowed_app_ids: Iterable[str] | None = None,
    ) -> None:
        data = CatalogData.model_validate_json(data_file.read_text(encoding="utf-8"))
        applications = list(data.apps)
        extra = Path(extra_directory) if extra_directory is not None else Path(data_file).parent / "apps.d"
        if extra.is_dir():
            for path in sorted(extra.glob("*.json")):
                extra_data = CatalogData.model_validate_json(path.read_text(encoding="utf-8"))
                applications.extend(extra_data.apps)
        app_ids = [app.id for app in applications]
        duplicates = sorted({app_id for app_id in app_ids if app_ids.count(app_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate app id across catalog files: {', '.join(duplicates)}")
        self._all_apps = tuple(applications)
        self._icon_directory = icon_directory.resolve()
        for app in self._all_apps:
            if app.icon_file is not None and self.icon_path(app) is None:
                raise ValueError(f"configured icon does not exist for app {app.id}: {app.icon_file}")

        if allowed_app_ids is None:
            self._allowed_app_ids: frozenset[str] | None = None
            exposed = self._all_apps
        else:
            requested = tuple(allowed_app_ids)
            if any(not isinstance(app_id, str) or not app_id for app_id in requested):
                raise ValueError("allowed_app_ids 必须是非空字符串列表")
            self._allowed_app_ids = frozenset(requested)
            known_ids = {app.id for app in self._all_apps}
            unknown = sorted(self._allowed_app_ids - known_ids)
            if unknown:
                raise ValueError(f"unknown app id in builtin.app_ids: {', '.join(unknown)}")
            exposed = tuple(app for app in self._all_apps if app.id in self._allowed_app_ids)
        self.apps = exposed
        self._apps_by_id = {app.id: app for app in self.apps}

    @property
    def allowed_app_ids(self) -> frozenset[str] | None:
        """配置的白名单；``None`` 表示不限制（暴露完整目录）。"""

        return self._allowed_app_ids

    @property
    def all_apps(self) -> tuple[AppDefinition, ...]:
        """完整目录（含白名单外的条目），用于诊断与统计。"""

        return self._all_apps

    @property
    def app_count(self) -> int:
        """当前对外暴露的软件数量（= ``len(list_apps(...))``）。"""

        return len(self.apps)

    @property
    def all_app_count(self) -> int:
        """加载到的完整目录条目数（含白名单外条目）。"""

        return len(self._all_apps)

    def get_app(self, app_id: str) -> AppDefinition | None:
        return self._apps_by_id.get(app_id)

    def list_apps(
        self, search: str | None, category: str | None, sort: Literal["recommended", "name"]
    ) -> list[AppDefinition]:
        query = (search or "").strip().casefold()
        selected_category = (category or "").strip()
        results = [
            app
            for app in self.apps
            if (not selected_category or app.category == selected_category)
            and (not query or query in self._search_text(app))
        ]
        if sort == "name":
            results.sort(key=lambda app: (app.name.casefold(), app.id))
        else:
            results.sort(key=lambda app: (not app.is_recommended, app.sort_order, app.name.casefold(), app.id))
        return results

    @staticmethod
    def _search_text(app: AppDefinition) -> str:
        return " ".join(
            [app.name, app.description, app.long_description, app.category, app.publisher, *app.features, *app.tags]
        ).casefold()

    @staticmethod
    def get_release(app: AppDefinition, release_id: str) -> Release | None:
        return next((release for release in app.releases if release.id == release_id), None)

    def icon_path(self, app: AppDefinition) -> Path | None:
        if app.icon_file is None:
            return None
        icon_path = (self._icon_directory / app.icon_file).resolve()
        if not icon_path.is_relative_to(self._icon_directory) or not icon_path.is_file():
            return None
        return icon_path
