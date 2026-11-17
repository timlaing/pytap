"""Bundled brand assets and native HA serving (no custom brand endpoint)."""

from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import make_mocked_request
from homeassistant.components.brands import BrandsIntegrationView
from homeassistant.components.http.const import KEY_AUTHENTICATED
from homeassistant.loader import async_get_integration
from PIL import Image
import pytest

from custom_components.pytap.const import DOMAIN

BRAND_DIR = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN / "brand"
ASSETS = {
    "icon.png": (256, 256),
    "icon@2x.png": (512, 512),
    "logo.png": (594, 256),
    "logo@2x.png": (1188, 512),
}


@pytest.mark.parametrize(("filename", "size"), ASSETS.items())
def test_brand_image_is_valid_transparent_png(filename, size):
    path = BRAND_DIR / filename
    assert not path.is_symlink()
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        assert image.format == "PNG"
        assert image.size == size
        assert image.convert("RGBA").getchannel("A").getextrema() == (0, 255)


@pytest.mark.parametrize(
    ("requested", "served"),
    [
        *((filename, filename) for filename in ASSETS),
        ("dark_icon.png", "icon.png"),
        ("dark_icon@2x.png", "icon@2x.png"),
        ("dark_logo.png", "logo.png"),
        ("dark_logo@2x.png", "logo@2x.png"),
    ],
)
async def test_ha_serves_local_brand_before_cdn(hass, requested, served):
    integration = await async_get_integration(hass, DOMAIN)
    assert integration.has_branding
    hass.data["brands"] = deque()
    request = make_mocked_request("GET", f"/api/brands/integration/{DOMAIN}/{requested}")
    request[KEY_AUTHENTICATED] = True
    view = BrandsIntegrationView(hass)
    with patch.object(view, "_serve_from_cache_or_cdn", new=AsyncMock()) as cdn:
        response = await view.get(request, DOMAIN, requested)
    assert response.status == 200
    assert response.content_type == "image/png"
    assert response.body == (BRAND_DIR / served).read_bytes()
    cdn.assert_not_called()
