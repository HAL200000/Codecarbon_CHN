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

PROXY = "http://127.0.0.1:7890"

PROXIES = {
    "http": PROXY,
    "https": PROXY,
}

# 电网区域映射字典
GRID_AREA_MAPPING = {
    "beijing": "华北", "tianjin": "华北", "hebei": "华北", "shanxi": "华北",
    "shandong": "华北", "neimenggu": "华北", "liaoning": "东北", "jilin": "东北",
    "heilongjiang": "东北", "shanghai": "华东", "jiangsu": "华东", "zhejiang": "华东",
    "anhui": "华东", "fujian": "华东", "henan": "华中", "hubei": "华中",
    "hunan": "华中", "jiangxi": "华中", "shaanxi": "西北", "gansu": "西北",
    "qinghai": "西北", "ningxia": "西北", "xinjiang": "西北", "guangdong": "南方",
    "guangxi": "南方", "yunnan": "南方", "guizhou": "南方", "hainan": "南方",
    "sichuan": "西南", "chongqing": "西南"
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
        if data:
            # ---- 同步经纬度、国家、省份 ----
            self.latitude  = data.get("latitude") or data.get("lat")
            self.longitude = data.get("longitude") or data.get("lon")
            self.country_name = data.get("country")
            # geojs: country_code3 / ip-api: countryCode
            self.country_iso_code = (
                data.get("country_code3") or data.get("countryCode") or self.country_iso_code
            )
            # geojs: region / ip-api: regionName
            self.region = (data.get("region") or data.get("regionName") or self.region or "").lower()

            # ---- 中国境内：按省份映射电网区域 ----
            if self.country_name and "china" in self.country_name.lower():
                if not self.region and self.latitude and self.longitude:
                    try:
                        # 主动用经纬度进行反查
                        loc = self._geolocator.reverse((self.latitude, self.longitude))
                        addr = loc.raw.get("address", {}) if loc else {}
                        self.region = addr.get("state", "").lower()
                    except Exception as e:
                        logger.warning(f"[GeoMetadata] Nominatim 反查失败: {e}")

                # 尝试匹配省份
                for province, grid_area in GRID_AREA_MAPPING.items():
                    if province in (self.region or ""):
                        return grid_area
                return "未知"

        # ---------- 3. 两个 IP‑API 都失败时兜底 ----------
        try:
            if self.latitude and self.longitude:
                loc = self._geolocator.reverse((self.latitude, self.longitude))
                addr = loc.raw.get("address", {}) if loc else {}
                self.country_name = self.country_name or addr.get("country")
                self.region = self.region or addr.get("state", "").lower()

                if self.country_name and "china" in self.country_name.lower():
                    for province, grid_area in GRID_AREA_MAPPING.items():
                        if province in self.region:
                            return grid_area
            return "未知"
        except Exception as e:
            logger.warning(f"[GeoMetadata] Nominatim 兜底失败: {e}")
            return "未知"

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
