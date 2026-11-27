"""Registered live backends. Keep launcher imports light and selection explicit."""


def mock_transport():
    from live_session import MockLiveTransport
    return MockLiveTransport()


class LiveSetupError(ValueError):
    """Safe to display. Retrying requires a configuration change."""


def gemini_transport(config):
    from gemini_live import GeminiLiveTransport
    if config.get('activity_allow_remote') is not True:
        raise LiveSetupError('Разреши отправку текстового контекста в Gemini в настройках Live Engine.')
    key = config.get('google_assistant_api_key')
    if not isinstance(key, str) or not key.strip():
        raise LiveSetupError('Введи API-ключ Gemini в настройках Live Engine.')
    return GeminiLiveTransport(key.strip(), allow_commentary=config.get('live_proactive') is True,
                               allow_images=config.get('activity_send_remote_images') is True)


def live_factory(config):
    mode = config.get('activity_provider_mode')
    if mode == 'gemini_live':
        return lambda: gemini_transport(config)
    if mode == 'live_mock':
        return mock_transport
    return None

