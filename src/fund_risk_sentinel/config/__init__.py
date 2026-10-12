"""配置层：路径解析、应用设置、模型服务配置。

.. code-block:: python

    from fund_risk_sentinel.config import get_manager, write_default_configs

    write_default_configs()          # 首次运行时生成示例配置
    config = get_manager()
    print(config.describe())
    provider = config.provider()     # 当前激活的模型配置
"""

from .paths import (
    APP_DIR_NAME,
    bundled_dir,
    config_search_paths,
    ensure_dir,
    is_frozen,
    project_root,
    resolve_config_file,
    user_config_dir,
    user_data_dir,
    user_log_dir,
)
from .settings import (
    DEFAULT_PROVIDERS_YAML,
    DEFAULT_SETTINGS_YAML,
    PROVIDERS_FILE,
    SETTINGS_FILE,
    AppSettings,
    ConfigManager,
    ProviderConfig,
    ProvidersConfig,
    ReviewSettings,
    SecuritySettings,
    UiSettings,
    get_manager,
    interpolate_env,
    set_manager,
    write_default_configs,
)

__all__ = [
    # paths
    "APP_DIR_NAME",
    "is_frozen",
    "project_root",
    "bundled_dir",
    "user_config_dir",
    "user_data_dir",
    "user_log_dir",
    "ensure_dir",
    "config_search_paths",
    "resolve_config_file",
    # settings
    "SETTINGS_FILE",
    "PROVIDERS_FILE",
    "ProviderConfig",
    "ProvidersConfig",
    "UiSettings",
    "ReviewSettings",
    "SecuritySettings",
    "AppSettings",
    "ConfigManager",
    "get_manager",
    "set_manager",
    "interpolate_env",
    "write_default_configs",
    "DEFAULT_SETTINGS_YAML",
    "DEFAULT_PROVIDERS_YAML",
]
