"""
网易大神小程序 - 集卡活动自动化脚本
抓包方法:
1. 使用 Proxyman/Charles/Fiddler 等工具代理微信
2. 打开"网易大神"小程序，进入集卡活动页面
3. 找到 inf-miniapp.ds.163.com 的请求
4. 从请求头中提取 GL-Token, GL-Uid, GL-DeviceId
配置:
1. NARAKA_SIGN_API_URL - 签名计算接口地址（必须）
2. NARAKA_TOKEN - 账号配置（TOKEN#UID#DEVICE_ID#name） name 可选
在请求头中找到以下对应关系：
TOKEN：对应 GL-Token 的值
UID：对应 GL-Uid 的值
DEVICE_ID：对应 GL-DeviceId 的值
=============================================================================
"""
import json
import time
import requests
import os
from typing import Optional, List, Dict, Any, Tuple

try:
    from notify import send as notify_send  # 青龙面板通知
except Exception:
    notify_send = None

# =============================================================================
# 配置
# =============================================================================
# 签名计算 API 地址（Cloudflare Worker）
SIGN_API_URL = os.environ.get("NARAKA_SIGN_API_URL", "https://your-worker.workers.dev/api/sign")

# 当前活动的卡册ID（可选；不填则脚本自动发现最新活动）
CARD_BOOK_ID = os.environ.get("NARAKA_CARD_BOOK_ID", "").strip()
_CARD_BOOK_ID_AUTO_LOGGED = False
# 是否开启账号间互相送卡（True 开启，False 关闭）
EXCHANGE_CARDS = os.environ.get("NARAKA_EXCHANGE_CARDS", "True").lower() == "true"
# =============================================================================


def send_notify(title: str, content: str) -> None:
    if not notify_send:
        return
    try:
        notify_send(title, content)
    except Exception as e:
        print(f"[notify] 发送失败: {e}")


class DSAutomator:
    def __init__(self, token: str, uid: str, device_id: str, name: str = ""):
        self.token = token
        self.uid = uid
        self.device_id = device_id
        self.name = name or uid[:8]  # 用于日志标识
        # --- 固定参数 ---
        self.base_url = "https://inf-miniapp.ds.163.com"
        # --- 动态获取的参数（初始化后填充）---
        self.app_key = ""
        self.role_id = ""
        self.server = ""
        self.act_id = ""
        self.card_as_id = ""
        self.luck_draw_as_id = ""
        # --- 缓存 ---
        self._role_info: Optional[Dict[str, Any]] = None
        self._act_config: Optional[Dict[str, Any]] = None
        self._initialized: bool = False
        # --- Session ---
        self.session = requests.Session()
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "GL-ClientType": "52",
            "GL-Source": "THIRD_WX",
            "GL-Channel": "god_wx53eacbe0d8a7a95a",
            "GL-DeviceId": self.device_id,
            "GL-Token": self.token,
            "GL-Uid": self.uid,
            "Referer": "https://servicewechat.com/wx53eacbe0d8a7a95a/324/page-frame.html"
        }

    def _get_sign_from_api(self, body_str: str) -> Optional[Dict[str, str]]:
        """
        调用远程 API 获取签名。
        
        Returns:
            {"nonce": "...", "checksum": "..."} 或 None
        """
        try:
            resp = self.session.post(
                SIGN_API_URL,
                json={
                    "device_id": self.device_id,
                    "token": self.token,
                    "uid": self.uid,
                    "body": body_str
                },
                timeout=10
            )
            data = resp.json()
            if data.get("ok"):
                return {
                    "nonce": data.get("nonce"),
                    "checksum": data.get("checksum")
                }
            else:
                print(f"[签名API] 错误: {data.get('error')}")
                return None
        except Exception as e:
            print(f"[签名API] 请求失败: {e}")
            return None

    def request(self, method: str, endpoint: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """发起请求，签名通过远程 API 计算"""
        url = f"{self.base_url}{endpoint}"
        body_str = json.dumps(body, separators=(',', ':'))
        
        # 调用 API 获取签名
        sign_data = self._get_sign_from_api(body_str)
        if not sign_data:
            return {"code": -1, "errmsg": "签名获取失败"}
        
        headers = self.headers.copy()
        headers["GL-Nonce"] = sign_data["nonce"]
        headers["GL-CheckSum"] = sign_data["checksum"]
        
        response = self.session.request(method, url, data=body_str, headers=headers)
        res_json = response.json()
        if res_json.get("code") != 200:
            print(f"请求失败 [{endpoint}]: {res_json.get('errmsg', '未知错误')}")
        return res_json

    def initialize(self) -> bool:
        """
        初始化：动态获取所有必要参数。
        返回 True 表示初始化成功。
        """
        if self._initialized:
            return True
        
        # 1. 获取角色信息 (appKey, roleId, server 等)
        role = self.get_role_info()
        if not role:
            print(f"[{self.name}] 初始化失败: 无法获取角色信息")
            return False

        # 2. 自动发现卡册ID（如未配置）
        global CARD_BOOK_ID, _CARD_BOOK_ID_AUTO_LOGGED
        if not CARD_BOOK_ID:
            discovered = self.discover_latest_card_book_id()
            if not discovered:
                print(f"[{self.name}] 初始化失败: 无法自动发现卡册ID（请稍后重试或手动设置 NARAKA_CARD_BOOK_ID）")
                return False
            CARD_BOOK_ID = discovered
            if not _CARD_BOOK_ID_AUTO_LOGGED:
                print(f"[活动配置] 已自动发现 cardBookId: {CARD_BOOK_ID}")
                _CARD_BOOK_ID_AUTO_LOGGED = True
        
        # 3. 获取活动配置 (actId, card_as_id)
        config = self.get_card_book_config()
        if not config:
            print(f"[{self.name}] 初始化失败: 无法获取活动配置")
            return False
        
        # 4. 动态获取模块 ID (抽奖、卡片等)
        modules = self.get_act_modules()
        for m in modules:
            m_id = m.get("asId")
            if not m_id:
                continue
            try:
                m_type_num = int(float(m.get("asType")))
            except (TypeError, ValueError):
                continue
            # 小程序侧是 find(asType===2)，取第一个；这里也保持一致
            if m_type_num == 2 and not self.luck_draw_as_id:
                self.luck_draw_as_id = m_id
            elif m_type_num == 43 and not self.card_as_id:
                self.card_as_id = m_id
        
        self._initialized = True
        return True

    def discover_latest_card_book_id(self) -> str:
        """
        自动获取最新卡册ID。

        通过 cardBookInfos 拉取卡册列表，取第一页第一条作为“最新卡册”。
        """
        body = {
            "appKey": self.app_key or "d90",
            "pageNum": 0,
            "pageSize": 1
        }
        res = self.request("POST", "/v1/miniapp/act/module/interchgCard/cardBookInfos", body)
        result = res.get("result") or {}
        books = result.get("books") or []
        if not books:
            return ""

        book0 = books[0] or {}
        base_info = book0.get("baseInfo") or {}
        return (base_info.get("id") or book0.get("id") or "").strip()

    def get_card_book_config(self) -> Optional[Dict[str, Any]]:
        """
        从 cardBookDetail 获取活动配置（actId, card_as_id）。
        """
        if self._act_config:
            return self._act_config
        
        body = {
            "cardBookId": CARD_BOOK_ID,
            "appKey": self.app_key,
            "roleId": self.role_id,
            "server": self.server
        }
        res = self.request("POST", "/v1/miniapp/act/module/interchgCard/cardBookDetail", body)
        result = res.get("result")
        if not result:
            return None
        
        self.act_id = result.get("actId", "")
        self.card_as_id = result.get("asId", "")
        
        self._act_config = result
        return result

    def get_bind_role_list(self) -> List[Dict[str, Any]]:
        """
        获取绑定的角色列表（动态获取角色信息）。
        返回所有绑定到当前账号的游戏角色。
        """
        body = {}
        res = self.request("POST", "/v1/miniapp/game/role/getBindList", body)
        result = res.get("result")
        
        if result is None:
            return []
        
        # 如果 result 直接是列表
        if isinstance(result, list):
            return result
        
        # 如果是字典，尝试不同的 key
        if isinstance(result, dict):
            return (
                result.get("appRoleList") or 
                result.get("roleList") or 
                result.get("list") or 
                []
            )
        
        return []

    def get_role_info(self, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        """
        获取当前角色详细信息。
        """
        if self._role_info and not force_refresh:
            return self._role_info
        
        role_list = self.get_bind_role_list()
        
        if not role_list:
            print(f"[{self.name}] 警告: 无法获取角色列表")
            return None
        
        # 优先查找 d90 (永劫无间) 的角色
        d90_role = next((r for r in role_list if r.get("appKey") == "d90"), None)
        if d90_role:
            self._update_from_role(d90_role)
            return d90_role
        
        # 没有 d90 角色，使用第一个
        self._update_from_role(role_list[0])
        return self._role_info

    def _update_from_role(self, role: Dict[str, Any]):
        """从角色信息更新实例属性"""
        self.role_id = role.get("roleId") or role.get("role_id") or self.role_id
        self.server = role.get("server") or self.server
        self.app_key = role.get("appKey") or role.get("app_key") or self.app_key or "d90"
        self._role_info = role

    def _build_act_role_info(self) -> Dict[str, Any]:
        """
        构建活动请求中的 actRoleInfo 参数（动态获取）。
        """
        role = self.get_role_info()
        if not role:
            # 如果获取失败，返回最小必需信息
            return {
                "appKey": self.app_key,
                "roleId": self.role_id,
                "server": self.server
            }
        
        return {
            "roleLevel": role.get("roleLevel") or role.get("level") or 0,
            "serverName": role.get("serverName") or role.get("server_name") or "",
            "nick": role.get("nick") or role.get("roleName") or "",
            "icon": role.get("icon") or "",
            "lastModified": role.get("lastModified") or int(time.time() * 1000),
            "appKey": self.app_key,
            "roleId": self.role_id,
            "server": self.server
        }

    def get_act_modules(self) -> List[Dict[str, Any]]:
        """获取活动所有模块信息"""
        body = {
            "actId": self.act_id,
            "ignoreFilterValidTime": True,
            "appKey": self.app_key,
            "roleId": self.role_id,
            "server": self.server
        }
        res = self.request("POST", "/v1/miniapp/act/module/common/actInfo", body)
        return res.get("result", {}).get("moduleList", [])

    def get_tasks(self):
        # 1. 动态获取所有模块并筛选任务模块 (asType=4)
        modules = self.get_act_modules()
        task_as_ids: List[str] = []
        for m in modules:
            as_id = m.get("asId")
            if not as_id:
                continue
            try:
                as_type_num = int(float(m.get("asType")))
            except (TypeError, ValueError):
                continue
            if as_type_num == 4:
                task_as_ids.append(as_id)
        
        if not task_as_ids:
            print(f"[{self.name}] 未在当前活动中找到任务模块")
            return []

        # 2. 动态获取角色信息
        role_info = self._build_act_role_info()
        
        base_body = {
            "actId": self.act_id,
            "asType": 4,
            # 动态角色信息
            "roleLevel": role_info.get("roleLevel", 0),
            "serverName": role_info.get("serverName", ""),
            "nick": role_info.get("nick", ""),
            "icon": role_info.get("icon", ""),
            "lastModified": role_info.get("lastModified", 0),
            # 基础信息
            "appKey": self.app_key,
            "roleId": self.role_id,
            "server": self.server,
            "visibleOSType": "ANDROID",
            "visiblePrdType": "MINI_PROGRAM"
        }

        all_tasks: List[Dict[str, Any]] = []
        seen_task_ids: set = set()
        for as_id in task_as_ids:
            body = base_body.copy()
            body["asIdList"] = [as_id]
            res = self.request("POST", "/v1/miniapp/act/task/taskInfo", body)
            task_list = (res.get("result") or {}).get("taskList") or []
            for t in task_list:
                task_id = t.get("asId") or t.get("id")
                if task_id and task_id in seen_task_ids:
                    continue
                if task_id:
                    seen_task_ids.add(task_id)
                all_tasks.append(t)

        return all_tasks

    def get_draw_info(self, luck_draw_as_id=None):
        luck_draw_as_id = luck_draw_as_id or self.luck_draw_as_id
        body = {
            "actId": self.act_id,
            "asId": luck_draw_as_id,
            "asType": 2,
            "appKey": self.app_key,
            "roleId": self.role_id,
            "server": self.server,
            "visibleOSType": "ANDROID",
            "visiblePrdType": "MINI_PROGRAM",
        }
        res = self.request("POST", "/v1/miniapp/act/module/luckDraw/luckDrawInfo", body)
        return res.get("result", {})

    def visit_activity(self, card_as_id=None):
        card_as_id = card_as_id or self.card_as_id
        if not self.act_id or not card_as_id:
            return {}
        body = {
            "actId": self.act_id,
            "asId": card_as_id,
            "asType": 43
        }
        return self.request("POST", "/v1/miniapp/act/module/interchgCard/collectInfo", body)

    def get_my_cards(self, card_as_id=None):
        card_as_id = card_as_id or self.card_as_id
        body = {
            "actId": self.act_id,
            "asId": card_as_id,
            "asType": 43, 
            "appKey": self.app_key,
            "roleId": self.role_id,
            "server": self.server
        }
        res = self.request("POST", "/v1/miniapp/act/module/interchgCard/myCard", body)
        return res.get("result", {})

    def share_card(self):
        body = {
            "asType": 43,
            "asId": self.card_as_id,
            "actId": self.act_id
        }
        return self.request("POST", "/v1/miniapp/act/module/interchgCard/shareCard", body)

    def post_give_wish(self, card_id: str) -> Dict[str, Any]:
        """
        发起赠送卡片请求。
        
        Args:
            card_id: 要赠送的卡片ID
            
        Returns:
            包含 interchangeWishId 的结果，用于接收方领取
        """
        body = {
            "asType": 43,
            "actId": self.act_id,
            "asId": self.card_as_id,
            "cardId": card_id
        }
        res = self.request("POST", "/v1/miniapp/act/module/interchgCard/postGiveWish", body)
        return res

    def accept_give_wish(self, wish_id: str) -> Dict[str, Any]:
        """
        领取他人赠送的卡片。
        
        Args:
            wish_id: 赠送方发起赠送后返回的 interchangeWishId
            
        Returns:
            领取结果
        """
        body = {
            "asType": 43,
            "actId": self.act_id,
            "asId": self.card_as_id,
            "interchangeWishId": wish_id
        }
        res = self.request("POST", "/v1/miniapp/act/module/interchgCard/acceptGiveWish", body)
        return res

    def get_giftable_cards(self) -> List[Dict[str, Any]]:
        """
        获取可赠送的卡片（数量 > 1 的卡片，保留1张自用）。
        """
        card_data = self.get_my_cards()
        card_infos = card_data.get('cardInfos', [])
        giftable = []
        for c in card_infos:
            num = c.get('num', 0) or 0
            if num > 1:  # 多于1张才能送
                giftable.append({
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "num": num,
                    "can_give": num - 1  # 可赠送数量
                })
        return giftable

    def get_missing_cards(self) -> List[Dict[str, Any]]:
        """
        获取缺少的卡片（数量 = 0 的卡片）。
        """
        card_data = self.get_my_cards()
        card_infos = card_data.get('cardInfos', [])
        missing = []
        for c in card_infos:
            num = c.get('num', 0) or 0
            if num == 0:
                missing.append({
                    "id": c.get("id"),
                    "name": c.get("name")
                })
        return missing

    def do_task(self, task_as_id):
        body = {
            "actId": self.act_id,
            "asIdList": [task_as_id],
            "asType": 4,
            "appKey": self.app_key,
            "roleId": self.role_id,
            "server": self.server
        }
        res = self.request("POST", "/v1/miniapp/act/task/doMultiActTask", body)
        return res

    def apply_prize(self, task_as_id):
        body = {
            "actId": self.act_id,
            "asId": task_as_id,
            "asType": 4,
            "appKey": self.app_key,
            "roleId": self.role_id,
            "server": self.server
        }
        return self.request("POST", "/v1/miniapp/act/task/applyTaskPrize", body)

    def draw(self, luck_draw_as_id=None):
        luck_draw_as_id = luck_draw_as_id or self.luck_draw_as_id
        body = {
            "actId": self.act_id,
            "asId": luck_draw_as_id,
            "asType": 2,
            "appKey": self.app_key,
            "roleId": self.role_id,
            "server": self.server,
            "visibleOSType": "ANDROID",
            "visiblePrdType": "MINI_PROGRAM",
        }
        res = self.request("POST", "/v1/miniapp/act/module/luckDraw/draw", body)
        return res.get("result", {})


def parse_accounts_from_env() -> List[Tuple[str, str, str, str]]:
    env_value = os.environ.get("NARAKA_TOKEN", "").strip()
    if not env_value:
        return []
    
    accounts = []
    # 支持 & 或换行符分隔多账号
    lines = env_value.replace("&", "\n").split("\n")
    
    for idx, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        
        # 支持 # 或 @ 作为字段分隔符
        sep = "#" if "#" in line else "@"
        parts = line.split(sep)
        
        if len(parts) < 3:
            print(f"[警告] 第{idx}行账号格式错误，至少需要 TOKEN{sep}UID{sep}DEVICE_ID")
            continue
        
        token = parts[0].strip()
        uid = parts[1].strip()
        device_id = parts[2].strip()
        name = parts[3].strip() if len(parts) > 3 else f"账号{idx}"
        
        accounts.append((token, uid, device_id, name))
    
    return accounts


if __name__ == "__main__":
    ACCOUNTS = parse_accounts_from_env()
    
    if not ACCOUNTS:
        print("[error] 未配置账号信息，请设置环境变量 NARAKA_TOKEN")
        print("[info] 格式: TOKEN#UID#DEVICE_ID#名称，多个账号用 & 分隔")
        exit(1)
        
    print(f"[青龙面板] 从环境变量 NARAKA_TOKEN 读取到 {len(ACCOUNTS)} 个账号")
    
    # 检查签名 API 是否配置
    if SIGN_API_URL == "https://your-worker.workers.dev/api/sign":
        print("[error] 未配置签名 API 地址，请设置环境变量 NARAKA_SIGN_API_URL")
        print("[info] 示例: export NARAKA_SIGN_API_URL='https://xxx.workers.dev/api/sign'")
        exit(1)
    
    print(f"[签名API] {SIGN_API_URL}")

    # 卡册ID：可选（未配置将自动发现）
    if CARD_BOOK_ID:
        print(f"[活动配置] cardBookId: {CARD_BOOK_ID}")
    # =============================================================================

    def create_bot(account_tuple) -> DSAutomator:
        """根据账号元组创建 DSAutomator 实例"""
        token, uid, device_id, name = account_tuple
        return DSAutomator(token, uid, device_id, name)

    def run_daily_tasks(bot: DSAutomator):
        """执行单个账号的每日任务"""
        # 初始化（动态获取所有参数）
        if not bot.initialize():
            print(f"[{bot.name}] 初始化失败，跳过此账号")
            return

        # 获取角色信息，用角色名作为显示名称
        role_info = bot.get_role_info()
        nick = "未知"
        if role_info:
            nick = role_info.get("nick") or role_info.get("roleName") or bot.name
            level = role_info.get("roleLevel") or role_info.get("level") or 0
            server_name = role_info.get("serverName") or role_info.get("server_name") or "未知"
        
        print(f"\n{'='*60}")
        print(f"[{nick}] 开始执行每日任务")
        print(f"{'='*60}")
        
        if role_info:
            print(f"角色: {nick} | 等级: Lv.{level} | 服务器: {server_name}")
            print(f"动态参数: appKey={bot.app_key}, roleId={bot.role_id[:16]}..., actId={bot.act_id[:16]}...")
        else:
            print("警告: 无法获取角色信息，将使用默认配置")

        # 分享卡片增加机会
        bot.share_card()

        # 执行任务
        print(f"\n[{nick}] --- 任务列表 ---")
        def is_visit_activity_task(task: Dict[str, Any]) -> bool:
            title = (task.get("title") or "")
            return "访问" in title and "活动" in title

        def is_send_card_task(task: Dict[str, Any]) -> bool:
            title = (task.get("title") or "")
            return "\u9001\u51fa" in title and "\u5361" in title

        tasks = bot.get_tasks()
        if any(is_visit_activity_task(t) and not t.get("completed") for t in tasks):
            bot.visit_activity()
            tasks = bot.get_tasks()
        for task in tasks:
            if task.get("alreadyGot"):
                continue
            status = "已完成" if task.get("completed") else "未开始"
            reward_got = "已领取" if task.get("alreadyGot") else "未领取"
            print(f"任务: {task.get('title')} | 状态: {status} | 奖励: {reward_got}")
            
            if not task.get("completed"):
                if is_send_card_task(task):
                    continue
                do_res = bot.do_task(task.get("asId"))
                print(f"  -> 任务执行: {do_res.get('errmsg', '成功')}")
                # 执行任务后立即尝试领取奖励（任务可能已完成）
                time.sleep(0.5)
                prize_res = bot.apply_prize(task.get("asId"))
                if prize_res.get('code') == 200:
                    print(f"  -> 奖励领取: OK")
                continue
                
            if task.get("completed") and not task.get("alreadyGot"):
                prize_res = bot.apply_prize(task.get("asId"))
                print(f"  -> 奖励领取: {prize_res.get('errmsg', '成功')}")

        # 抽奖
        print(f"\n[{nick}] --- 开始抽奖 ---")
        if not bot.luck_draw_as_id:
            print(f"[{nick}] 未获取到抽奖模块ID(asId)，跳过抽奖")
            return
        print(f"[{nick}] 抽奖模块 asId: {bot.luck_draw_as_id}")
        win_prizes: List[str] = []
        while True:
            draw_info = bot.get_draw_info()
            chances = draw_info.get('myLeftDrawChance', 0)
            if chances <= 0:
                print("没有剩余抽奖机会。")
                break
            
            res = bot.draw()
            if res.get("isWin"):
                prize = res.get("winPrize", {})
                prize_name = prize.get("prizeName") or prize.get("name") or "未知奖品"
                win_prizes.append(prize_name)
                print(f"恭喜！抽到: {prize_name}")
            else:
                print("此次未中奖。")
            time.sleep(1)

        if win_prizes:
            send_notify(
                "集卡抽奖中奖",
                f"{nick}抽到:\n" + "\n".join(f"- {p}" for p in win_prizes),
            )

        # 显示卡片状态
        print(f"\n[{nick}] --- 卡片状态 ---")
        card_data = bot.get_my_cards()
        card_infos = card_data.get('cardInfos', [])
        owned = [f"{c.get('name')}({c.get('num', 0)})" for c in card_infos if (c.get('num') or 0) > 0]
        missing = [c.get('name') for c in card_infos if (c.get('num') or 0) == 0]
        print(f"已拥有: {', '.join(owned) if owned else '无'}")
        print(f"缺少: {', '.join(missing) if missing else '无'}")

    def pair_exchange_cards(bot_a: DSAutomator, bot_b: DSAutomator):
        """
        两个账号互相赠送卡片。
        策略: 
        1. A 有多余的且 B 缺少的卡 -> A 送给 B
        2. B 有多余的且 A 缺少的卡 -> B 送给 A
        3. 如果没有缺少的卡，就互相送数量最多的卡（为完成任务获取抽奖机会）
        """
        # 确保两个账号都初始化
        if not bot_a._initialized:
            bot_a.initialize()
        if not bot_b._initialized:
            bot_b.initialize()

        # 获取角色名作为显示名称
        a_role = bot_a.get_role_info()
        b_role = bot_b.get_role_info()
        a_nick = a_role.get("nick") or bot_a.name if a_role else bot_a.name
        b_nick = b_role.get("nick") or bot_b.name if b_role else bot_b.name

        print(f"\n{'='*60}")
        print(f"[配对赠送] {a_nick} <-> {b_nick}")
        print(f"{'='*60}")

        # 获取双方的卡片信息
        a_giftable = bot_a.get_giftable_cards()
        a_missing = bot_a.get_missing_cards()
        b_giftable = bot_b.get_giftable_cards()
        b_missing = bot_b.get_missing_cards()

        print(f"\n[{a_nick}] 可赠送: {[c['name'] + '(' + str(c['num']) + ')' for c in a_giftable]}")
        print(f"[{a_nick}] 缺少: {[c['name'] for c in a_missing]}")
        print(f"[{b_nick}] 可赠送: {[c['name'] + '(' + str(c['num']) + ')' for c in b_giftable]}")
        print(f"[{b_nick}] 缺少: {[c['name'] for c in b_missing]}")

        def do_gift(sender: DSAutomator, receiver: DSAutomator, sender_nick: str, receiver_nick: str, card: Dict[str, Any], reason: str):
            """执行赠送"""
            print(f"\n[{sender_nick}] -> [{receiver_nick}] {reason}: {card['name']}")
            give_res = sender.post_give_wish(card['id'])
            if give_res.get('code') == 200 and give_res.get('result', {}).get('interchangeWishId'):
                wish_id = give_res['result']['interchangeWishId']
                print(f"  赠送发起成功, wishId: {wish_id[:16]}...")
                time.sleep(0.5)
                
                accept_res = receiver.accept_give_wish(wish_id)
                if accept_res.get('code') == 200:
                    print(f"  [{receiver_nick}] 领取成功!")
                    return True
                else:
                    print(f"  [{receiver_nick}] 领取失败: {accept_res.get('errmsg')}")
            else:
                print(f"  赠送发起失败: {give_res.get('errmsg')}")
            return False

        a_sent = False  # A是否已送出
        b_sent = False  # B是否已送出

        # --- A 送给 B ---
        b_missing_ids = {c['id'] for c in b_missing}
        # 策略1: 优先送 B 缺少的卡
        for card in a_giftable:
            if card['id'] in b_missing_ids:
                if do_gift(bot_a, bot_b, a_nick, b_nick, card, "赠送缺少的卡"):
                    a_sent = True
                time.sleep(1)
                break
        
        # 策略2: 如果 B 不缺卡，送数量最多的（为了完成任务）
        if not a_sent and a_giftable:
            # 按数量排序，送最多的
            sorted_cards = sorted(a_giftable, key=lambda x: x['num'], reverse=True)
            card = sorted_cards[0]
            if do_gift(bot_a, bot_b, a_nick, b_nick, card, "赠送数量最多的卡(完成任务)"):
                a_sent = True
            time.sleep(1)

        # --- B 送给 A ---
        a_missing_ids = {c['id'] for c in a_missing}
        # 策略1: 优先送 A 缺少的卡
        for card in b_giftable:
            if card['id'] in a_missing_ids:
                if do_gift(bot_b, bot_a, b_nick, a_nick, card, "赠送缺少的卡"):
                    b_sent = True
                time.sleep(1)
                break
        
        # 策略2: 如果 A 不缺卡，送数量最多的（为了完成任务）
        if not b_sent and b_giftable:
            sorted_cards = sorted(b_giftable, key=lambda x: x['num'], reverse=True)
            card = sorted_cards[0]
            if do_gift(bot_b, bot_a, b_nick, a_nick, card, "赠送数量最多的卡(完成任务)"):
                b_sent = True
            time.sleep(1)

        # 总结
        print(f"\n[赠送结果] {a_nick}: {'已送出' if a_sent else '未送出'} | {b_nick}: {'已送出' if b_sent else '未送出'}")

    # =============================================================================
    # 主逻辑
    # =============================================================================
    
    # 创建所有 bot 实例
    bots = [create_bot(acc) for acc in ACCOUNTS]

    # 1. 按组配对互相赠送卡片 (1-2, 3-4, 5-6 ...)
    if EXCHANGE_CARDS:
        print(f"\n\n{'#'*60}")
        print("# 开始配对互相赠送卡片")
        print(f"{'#'*60}")
        
        for i in range(0, len(bots) - 1, 2):
            bot_a = bots[i]
            bot_b = bots[i + 1]
            try:
                pair_exchange_cards(bot_a, bot_b)
            except Exception as e:
                print(f"[{bot_a.name} <-> {bot_b.name}] 互赠出错: {e}")
            time.sleep(2)

        # 如果账号数量是奇数，最后一个账号没有配对
        if len(bots) % 2 == 1:
            print(f"\n[提示] {bots[-1].name} 是奇数账号，没有配对对象")
    else:
        print(f"\n\n[提示] 互赠卡片功能已关闭 (NARAKA_EXCHANGE_CARDS=False)")

    # 2. 再执行每个账号的每日任务
    for bot in bots:
        try:
            run_daily_tasks(bot)
        except Exception as e:
            print(f"[{bot.name}] 执行任务出错: {e}")
        time.sleep(2)

    print(f"\n{'='*60}")
    print("所有账号处理完成！")
    print(f"{'='*60}")
