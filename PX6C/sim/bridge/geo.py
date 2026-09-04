# -*- coding: utf-8 -*-
"""
좌표/대기 변환 유틸 — FDM(로컬 NED, 미터)과 MAVLink(WGS84 위경도, ISA 기압)
사이를 오갈 때 필요한 순수 함수만 모아둔 모듈. 다른 모듈에 대한 의존성이 없어
단독으로 테스트하기 쉽다.
"""
import math

# 500m x 500m 정도의 좁은 박스 안에서만 쓰므로, 지구 곡률을 무시한 평면 근사
# (equirectangular)로 충분하다 — 원점 위도에서의 위경도 1도당 거리(m)만 사용.
_M_PER_DEG_LAT = 111_320.0


def ned_to_latlon(north_m, east_m, origin_lat_deg, origin_lon_deg):
    """로컬 NED(원점 기준, m) -> WGS84 위경도(deg)."""
    m_per_deg_lon = _M_PER_DEG_LAT * math.cos(math.radians(origin_lat_deg))
    lat = origin_lat_deg + north_m / _M_PER_DEG_LAT
    lon = origin_lon_deg + east_m / max(1e-6, m_per_deg_lon)
    return lat, lon


def latlon_to_ned(lat_deg, lon_deg, origin_lat_deg, origin_lon_deg):
    """WGS84 위경도(deg) -> 로컬 NED(원점 기준, m). (역방향이 필요할 때 대비)"""
    m_per_deg_lon = _M_PER_DEG_LAT * math.cos(math.radians(origin_lat_deg))
    north = (lat_deg - origin_lat_deg) * _M_PER_DEG_LAT
    east = (lon_deg - origin_lon_deg) * m_per_deg_lon
    return north, east


# ── ISA 표준대기 (이 프로젝트의 기본 가정 — 별도 언급 없으면 항상 ISA 사용) ──
_ISA_P0_PA = 101_325.0
_ISA_T0_K = 288.15
_ISA_LAPSE_K_PER_M = 0.0065
_ISA_G = 9.80665
_ISA_M_AIR = 0.0289644
_ISA_R = 8.3144598


def isa_pressure_hpa(alt_m_amsl):
    """AMSL 고도(m) -> ISA 표준대기 절대기압(hPa). 대류권(11km 이하)만 근사."""
    if not math.isfinite(alt_m_amsl):
        # NaN은 min()/max()로 걸러지지 않고 그대로 통과해버리므로(NaN 비교는 항상
        # False) FDM이 발산해도 조용히 11,000m 취급되는 일이 없도록 여기서 명시적으로 막는다.
        alt_m_amsl = 0.0
    alt_m_amsl = max(-500.0, min(11_000.0, alt_m_amsl))
    t_ratio = 1.0 - (_ISA_LAPSE_K_PER_M * alt_m_amsl) / _ISA_T0_K
    exponent = (_ISA_G * _ISA_M_AIR) / (_ISA_R * _ISA_LAPSE_K_PER_M)
    pressure_pa = _ISA_P0_PA * (t_ratio ** exponent)
    return pressure_pa / 100.0


def isa_temperature_c(alt_m_amsl):
    """AMSL 고도(m) -> ISA 표준대기 기온(°C)."""
    if not math.isfinite(alt_m_amsl):
        alt_m_amsl = 0.0
    return (_ISA_T0_K - _ISA_LAPSE_K_PER_M * alt_m_amsl) - 273.15
