"""
Encapsulates external dependencies to retrieve cloud and geographical metadata
"""

import re
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Dict, Optional
from geopy.geocoders import Nominatim

import requests

from codecarbon.core.cloud import get_env_cloud_details
from codecarbon.external.logger import logger

import json
from pathlib import Path

# 假设 JSON 文件在当前目录
GRID_JSON_PATH = Path(__file__).parent / "grid_area_mapping.json"

with open(GRID_JSON_PATH, "r", encoding="utf-8") as f:
    GRID_AREA_MAPPING = json.load(f)


PROXY = "http://127.0.0.1:7890"

PROXIES = {
    "http": PROXY,
    "https": PROXY,
}

@dataclass
class CloudMetadata:
    provider: Optional[str]
    region: Optional[str]

    @property
    def is_on_private_infra(self) -> bool:
        return self.provider is None and self.region is None

    @classmethod
    def from_utils(cls) -> "CloudMetadata":
        def extract_gcp_region(zone: str) -> str:
            """
            projects/705208488469/zones/us-central1-a -> us-central1
            """
            google_region_regex = r"[a-z]+-[a-z]+[0-9]"
            return re.search(google_region_regex, zone).group(0)

        extract_region_for_provider: Dict[str, Callable] = {
            "aws": lambda x: x["metadata"].get("region"),
            "azure": lambda x: x["metadata"]["compute"].get("location"),
            "gcp": lambda x: extract_gcp_region(x["metadata"].get("zone")),
        }

        cloud_metadata: Dict = get_env_cloud_details()

        if cloud_metadata is None or cloud_metadata["metadata"] == {}:
            return cls(provider=None, region=None)

        provider: str = cloud_metadata["provider"].lower()
        region: str = extract_region_for_provider.get(provider)(cloud_metadata)
        if region is None:
            logger.warning(
                f"Cloud provider '{provider}' detected, but unable to read region. Using country value instead."
            )
        if provider in ["aws", "azure"]:
            logger.warning(
                f"Cloud provider '{provider}' do not publish electricity carbon intensity. Using country value instead."
            )
            provider = None
            region = None
        return cls(provider=provider, region=region)


class GeoMetadata:
    """自动通过 IP 定位经纬度，并判定中国境内电网区域"""

    # ---------- 初始化 ----------
    def __init__(
        self,
        country_iso_code: Optional[str] = None,
        country_name:     Optional[str] = None,
        region:           Optional[str] = None,
        latitude:         Optional[float] = None,
        longitude:        Optional[float] = None,
        country_2letter_iso_code: Optional[str] = None,
    ):
        self.country_iso_code  = country_iso_code.upper() if country_iso_code else None
        self.country_name      = country_name
        self.region            = region.lower() if region else None        # 省 / 州
        self.latitude          = latitude
        self.longitude         = longitude
        self.country_2letter_iso_code = (
            country_2letter_iso_code.upper() if country_2letter_iso_code else None
        )
        # Nominatim 只在最后兜底用一次
        self._geolocator = Nominatim(user_agent="codecarbon_geo", timeout=5)

    # ---------- 打印 ----------
    def __repr__(self) -> str:
        return (f"GeoMetadata(iso3={self.country_iso_code}, country={self.country_name}, "
                f"region={self.region}, lat={self.latitude}, lon={self.longitude})")

    # ---------- 1. 依次调用两个 IP‑Geo API ----------
    def _query_ip_location(self) -> Optional[dict]:
        urls = [
            "https://get.geojs.io/v1/ip/geo.json",  # 主
            "http://ip-api.com/json/"               # 备
        ]
        for url in urls:
            try:
                r = requests.get(url, timeout=8, proxies=PROXIES)
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                logger.debug(f"[GeoMetadata] {url} 失败: {e}")
        return None

    # ---------- 2. 主要接口：返回电网区域 ----------
    def get_grid_area(self) -> str:
        data = self._query_ip_location()
        BAIDU_AK = "IyaMPVrnoEFRghWqzanAawrY7ol2dh2K"

        if data:
            # ---- 同步基本地理信息 ----
            self.latitude  = data.get("latitude") or data.get("lat")
            self.longitude = data.get("longitude") or data.get("lon")
            self.country_name = data.get("country")
            self.country_iso_code = data.get("country_code3") or data.get("countryCode") or self.country_iso_code
            self.region = (data.get("region") or data.get("regionName") or self.region or "").lower()

        # ---- 百度 API 补全 region（如果缺）----
        if self.country_name and "china" in self.country_name.lower():
            if not self.region and self.latitude and self.longitude:
                try:
                    baidu_url = (
                        f"http://api.map.baidu.com/reverse_geocoding/v3/"
                        f"?ak={BAIDU_AK}&output=json&location={self.latitude},{self.longitude}"
                    )
                    res = requests.get(baidu_url, timeout=8)
                    res.raise_for_status()
                    result = res.json().get("result", {})
                    self.region = result.get("addressComponent", {}).get("province", "").lower()
                    logger.info(f"[GeoMetadata] 百度定位补充省份: {self.region}")
                except Exception as e:
                    logger.warning(f"[GeoMetadata] 百度API反查失败: {e}")
                    # Nominatim 再兜底
                    try:
                        loc = self._geolocator.reverse((self.latitude, self.longitude))
                        addr = loc.raw.get("address", {}) if loc else {}
                        self.region = addr.get("state", "").lower()
                        logger.info(f"[GeoMetadata] Nominatim 补充省份: {self.region}")
                    except Exception as e:
                        logger.warning(f"[GeoMetadata] Nominatim 反查失败: {e}")

        # ---- 最终统一匹配逻辑（中英文兼容）----
        if self.country_name and "china" in self.country_name.lower():
            region_key = (self.region or "").lower().replace("省", "").replace("市", "").replace("自治区", "").strip()
            grid_area = GRID_AREA_MAPPING.get(self.region) or GRID_AREA_MAPPING.get(region_key)
            return grid_area or "未知"
        else:
            return "非中国地区"



    # ---------- 4. 保留原有 from_geo_js（供 codecarbon 其他逻辑使用） ----------
    @classmethod
    def from_geo_js(cls, url: str) -> "GeoMetadata":
        try:
            res = requests.get(url, timeout=10, proxies=PROXIES).json()
            return cls(
                country_iso_code=res.get("country_code3"),
                country_name=res.get("country"),
                region=res.get("region"),
                latitude=float(res.get("latitude")),
                longitude=float(res.get("longitude")),
                country_2letter_iso_code=res.get("country_code"),
            )
        except Exception as e:
            logger.warning(f"[GeoMetadata] geojs 调用失败: {e}")
            # fallback - 尝试 ip-api
        try:
            res = requests.get("http://ip-api.com/json/", timeout=10, proxies=PROXIES).json()
            country_name = res["country"]
            # 查三字码
            iso3_url = f"https://api.first.org/data/v1/countries?q={urllib.parse.quote_plus(country_name)}&scope=iso"
            iso3     = requests.get(iso3_url, timeout=10, proxies=PROXIES).json()["data"]
            iso3_code = next(iter(iso3.keys()))
            return cls(
                country_iso_code=iso3_code,
                country_name=country_name,
                region=res.get("regionName"),
                latitude=float(res.get("lat")),
                longitude=float(res.get("lon")),
                country_2letter_iso_code=res.get("countryCode"),
            )
        except Exception as e:
            logger.warning(f"[GeoMetadata] 备份 ip-api 失败: {e} - 默认设为 Canada")
            return cls(
                country_iso_code="CAN",
                country_name="Canada",
                region="Quebec",
                latitude=46.8,
                longitude=-71.2,
                country_2letter_iso_code="CA",
            )
        
