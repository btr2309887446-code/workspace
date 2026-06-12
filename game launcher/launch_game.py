import os
import sys
import string
import ctypes
import time
import datetime

def launch_game(game_name):
    game_name = game_name.lower()
    
    # Windows 中常见的快捷方式存放位置
    locations = [
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
        os.path.expandvars(r"%PROGRAMDATA%\Microsoft\Windows\Start Menu\Programs"),
        os.path.expandvars(r"%USERPROFILE%\Desktop"),
        os.path.expandvars(r"%PUBLIC%\Desktop")
    ]
    
    # 动态获取所有盘符 (如 C:\, D:\ 等)
    drives = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for letter in string.ascii_uppercase:
        if bitmask & 1:
            drives.append(f"{letter}:\\")
        bitmask >>= 1
        
    # 定义常见的游戏/程序存放根目录
    common_folders = [
        "Program Files",
        "Program Files (x86)",
        "Games",
        "游戏",
        "SteamLibrary",
        "Steam",
        "Epic Games",
        "Origin Games",
        "Ubisoft Game Launcher",
        "GOG Galaxy",
        "WeGameApps",
        "XboxGames"
    ]
    
    # 拼接盘符和常见目录，如果存在则加入扫描列表
    for drive in drives:
        for folder in common_folders:
            path = os.path.join(drive, folder)
            if os.path.exists(path) and path not in locations:
                locations.append(path)
                
    print("正在深度扫描桌面、开始菜单及各大常见安装目录，这大概需要几秒钟，请稍候...")
    
    matches = []
    
    for loc in locations:
        if not os.path.exists(loc):
            continue
            
        # 遍历目录查找快捷方式
        for root, dirs, files in os.walk(loc):
            for file in files:
                # 寻找 .lnk (普通快捷方式) 或 .url (如 Steam 游戏快捷方式)
                if file.lower().endswith(('.lnk', '.url', '.exe')):
                    if game_name in file.lower():
                        matches.append(os.path.join(root, file))
                        
    # 过滤掉卸载程序等包含类似名称的无关文件
    filtered_matches = []
    for match in matches:
        lower_match = match.lower()
        if "uninstall" not in lower_match and "卸载" not in lower_match:
            filtered_matches.append(match)
            
    matches = list(set(filtered_matches)) # 去重
                        
    if not matches:
        print(f"未能找到名称包含 '{game_name}' 的相关游戏或程序（例如 .exe 等文件）。")
        print("请确认您的输入是否有误，或者它是否安装在一些非常规的自定义路径下。")
        return False
        
    print(f"找到 {len(matches)} 个相关的项:")
    for i, match in enumerate(matches):
        print(f"[{i + 1}] {os.path.basename(match)}")
        
    if len(matches) == 1:
        choice = 0
    else:
        while True:
            try:
                user_input = input("\n检测到多个匹配项，请输入要启动的编号 (按回车退出): ")
                if not user_input.strip():
                    return False
                choice = int(user_input) - 1
                if 0 <= choice < len(matches):
                    break
                else:
                    print("无效的编号，请重新输入。")
            except ValueError:
                print("请输入数字。")
                
    target = matches[choice]
    target_name = os.path.basename(target)
    
    # 增加二次确认装置
    confirm = input(f"\n确定要启动 {target_name} 吗？(y/n) [默认: y]: ")
    if confirm.strip().lower() not in ('', 'y', 'yes'):
        print("已取消启动。")
        return False
        
    schedule_time = input("\n[可选] 请输入定时启动的时间 (如 18:30) (直接回车为立即启动): ").strip()
    wait_seconds = 0
    if schedule_time:
        try:
            now = datetime.datetime.now()
            hour, minute = map(int, schedule_time.replace('：', ':').split(':'))
            target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target_time < now:
                target_time += datetime.timedelta(days=1)
            wait_seconds = (target_time - now).total_seconds()
        except Exception:
            print("时间格式有误，默认立即启动。")
            
    close_str = input("\n[可选] 请输入启动后几分钟自动关闭游戏？(直接回车为不自动关闭): ").strip()
    close_minutes = 0
    proc_name = ""
    if close_str:
        try:
            close_minutes = float(close_str)
            if target.lower().endswith('.exe'):
                proc_name = os.path.basename(target)
            else:
                proc_name = input(f"由于您选择了快捷方式，为了在 {close_minutes} 分钟后准确结束进程，请输入该游戏的进程名\n(比如 cs2.exe，如果不确定可直接回车，我们将尝试模糊匹配): ").strip()
                if not proc_name:
                    # 模糊匹配带有原游戏名词的exe
                    proc_name = f"*{game_name}*.exe"
        except ValueError:
            print("输入的时间有误，将不启用自动关闭。")

    if wait_seconds > 0:
        print(f"\n等待中... 将在 {target_time.strftime('%Y-%m-%d %H:%M')} 启动 (剩余 {int(wait_seconds)} 秒)")
        while wait_seconds > 0:
            time.sleep(1)
            wait_seconds -= 1
            
    print(f"\n正在启动: {target_name}...")
    try:
        # os.startfile 可以像用户双击一样运行 .lnk, .url 或 .exe
        os.startfile(target)
        print("启动成功！")
        
        if close_minutes > 0:
            print(f"\n已安排在 {close_minutes} 分钟后关闭游戏...")
            close_seconds = int(close_minutes * 60)
            while close_seconds > 0:
                time.sleep(1)
                close_seconds -= 1
                
            print(f"时间到！正在尝试自动结束游戏: {proc_name}")
            os.system(f'taskkill /F /IM "{proc_name}" /T >nul 2>&1')
            print("关闭指令已发送！")
            
        return True
    except Exception as e:
        print(f"操作失败: {e}")
        return False

if __name__ == "__main__":
    print("-" * 40)
    print("      游戏/程序 快速启动器")
    print("-" * 40)
    
    if len(sys.argv) > 1:
        name = " ".join(sys.argv[1:])
    else:
        name = input("请输入您要启动的游戏或程序名称: ")
        
    if name.strip():
        launch_game(name)
    else:
        print("未输入名称，程序退出。")
