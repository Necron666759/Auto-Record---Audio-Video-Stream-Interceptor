#!/bin/bash
# build-windows.sh
#
# Собирает Windows-пакеты программы (Auto Record - Audio Stream
# Interceptor, Suno-only), оставаясь на Linux — по образцу присланного
# build-windows.sh, но для этого конкретного проекта, и с ТРЕМЯ
# результатами вместо двух (просили не ограничиваться одним msi):
#
#   1) AutoRecord-portable.exe — один-единственный исполняемый файл
#      (PyInstaller --onefile), ничего не устанавливает, просто
#      запускается.
#   2) AutoRecord-Setup.msi    — Windows Installer пакет (через
#      msitools/wixl — свободная Linux-реализация части WiX Toolset).
#   3) AutoRecord-Setup.exe    — классический exe-инсталлятор (через
#      NSIS/makensis) с обычным wizard'ом ("Далее -> Далее -> Готово"),
#      ярлыками и штатной записью в "Установка и удаление программ"/
#      "Приложения и возможности". В отличие от wixl, makensis — нативный
#      Linux-инструмент, Wine ему тоже не нужен.
#
# Зачем сразу два инсталлятора (.msi И .exe), а не один: у них разная
# аудитория и разное поведение при повторной установке/групповых
# политиках —.msi лучше подходит для корпоративного/silent-разворачивания
# (msiexec /i ... /quiet, GPO), .exe — привычнее рядовому пользователю
# (просто дважды кликнуть) и не требует Windows Installer-специфичных
# прав в некоторых ограниченных профилях. Оба собираются из ОДНОГО и
# того же portable .exe (см. Часть 3) — отличаются только обёртка.
#
# Как это работает без Windows:
#   - Wine исполняет Windows PE-бинарники (включая сам python.exe) прямо
#     на Linux — нужен только для шага 1 (PyInstaller собирает .exe,
#     запускаясь под Wine).
#   - Вместо официального инсталлятора python.org используется
#     ПЕРЕНОСИМАЯ сборка Windows Python с GitHub Releases проекта
#     astral-sh/python-build-standalone — просто zip/tar с готовым
#     деревом python.exe + DLL, ничего "устанавливать" даже внутри Wine
#     не нужно, только распаковать.
#   - wixl (пакет 'wixl', из проекта msitools) собирает .msi из
#     .wxs-описания — нативный Linux-инструмент, Wine не нужен.
#   - makensis (пакет 'nsis') собирает .exe-инсталлятор из .nsi-скрипта —
#     тоже нативный Linux-инструмент, Wine не нужен.
#
# ============================================================================
# ЧЕСТНОЕ ПРЕДУПРЕЖДЕНИЕ ПРО ЗВУК (прочитайте перед использованием):
# ============================================================================
# Сама программа изначально написана под Linux и затем портирована на
# Windows отдельным слоем (win_audio_loopback.py, win_process.py) — он НЕ
# ПРОВЕРЕН на реальной Windows (в среде разработки её физически не было),
# особенно часть с захватом звука через WASAPI Process Loopback Capture
# (win_audio_loopback.py) — там прямая работа с COM/ctypes, которую
# невозможно протестировать без реальной Windows 10 (2004+)/11. См.
# подробные предупреждения в самом файле и в README_WINDOWS.md. Собранные
# этим скриптом .exe/.msi ЗАПУСТЯТСЯ, но перед боевым использованием
# стоит прогнать хотя бы один полный цикл записи на настоящей Windows и
# свериться с логом (а при проблемах активации Process Loopback —
# воспользоваться аварийным режимом --capture-mode device / чекбоксом в
# GUI, см. README_WINDOWS.md).
#
# Использование:
#   ./build-windows.sh                  собрать всё: portable exe + msi + exe-инсталлятор
#                                        (спросив про установку wine/msitools/nsis)
#   ./build-windows.sh --yes            то же самое, без вопросов
#   ./build-windows.sh --skip-deps      не трогать системные пакеты, только собрать
#   ./build-windows.sh --deps-only      только поставить wine/msitools/nsis
#   ./build-windows.sh --portable-only  собрать только .exe (без обоих инсталляторов)
#   ./build-windows.sh --installers-only   не пересобирать portable .exe, собрать
#                                           из уже готового только msi+exe-инсталлятор
#   ./build-windows.sh --no-msi         не собирать .msi (portable + exe-инсталлятор)
#   ./build-windows.sh --no-exe-installer  не собирать exe-инсталлятор (portable + msi)
#
# Требуется: Linux x86_64 (Debian/Ubuntu-семейство для автоустановки
# зависимостей — на других дистрибутивах поставьте wine64, msitools
# (wixl) и nsis вручную и запускайте с --skip-deps), интернет
# (Wine-пакеты, переносимый Windows Python, PyInstaller и его
# зависимости качаются из PyPI и GitHub Releases), ~3 ГБ свободного места.

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

ASSUME_YES=0
SKIP_DEPS=0
DEPS_ONLY=0
SKIP_PORTABLE=0
SKIP_MSI=0
SKIP_EXE_INSTALLER=0
for arg in "$@"; do
    case "$arg" in
        --yes|-y) ASSUME_YES=1 ;;
        --skip-deps) SKIP_DEPS=1 ;;
        --deps-only) DEPS_ONLY=1 ;;
        --portable-only) SKIP_MSI=1; SKIP_EXE_INSTALLER=1 ;;
        --installers-only) SKIP_PORTABLE=1 ;;
        --no-msi) SKIP_MSI=1 ;;
        --no-exe-installer) SKIP_EXE_INSTALLER=1 ;;
        --help|-h)
            sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Неизвестный флаг: $arg (см. ./build-windows.sh --help)" >&2
            exit 1
            ;;
    esac
done

APP_NAME="AutoRecord"
APP_DISPLAY_NAME="Auto Record - Audio Stream Interceptor"
APP_PUBLISHER="AutoRecord"
# "Человеческая" базовая версия — меняйте вручную при значимых релизах.
# Полный ProductVersion для MSI и DisplayVersion для exe-инсталлятора
# собираются НИЖЕ (см. Часть 4), третье поле — автоинкрементный номер
# сборки. ПОЧЕМУ ЭТО ВАЖНО: Windows Installer (<MajorUpgrade> в .wxs)
# считает "обновлением" ТОЛЬКО строго большую версию, чем уже
# установленная — при повторной установке с тем же ProductVersion, но
# новым (см. Product Id="*" ниже — генерируется заново на каждой сборке)
# ProductCode Windows Installer видит "другой продукт той же версии" и
# откажется ставить (обычно "Another version of this product is already
# installed"). Поэтому номер сборки автоинкрементный, а не фиксированный.
APP_VERSION_BASE="1.0"
# GUID продукта — ФИКСИРОВАННЫЙ, не меняется между версиями (и Windows
# Installer, и NSIS-деинсталлятор используют его как устойчивый
# идентификатор приложения; при генерации заново на каждой сборке у
# пользователей копились бы отдельные записи в "Установка и удаление
# программ" на каждую версию).
PRODUCT_UPGRADE_CODE="A17E5B9E-3C2A-4E5B-8F91-2D4B6C8A9E10"
# Отдельный (тоже фиксированный) идентификатор для ключа реестра
# деинсталлятора NSIS — сознательно не тот же GUID, что у MSI
# (PRODUCT_UPGRADE_CODE), чтобы .msi и .exe-инсталлятор были для Windows
# двумя независимыми записями в "Установка и удаление программ" (ставить
# оба одновременно — не сценарий использования, но и мешать друг другу
# они не должны, если это всё же произойдёт).
NSIS_UNINSTALL_KEY="AutoRecordAudioStreamInterceptor"

BUILD_DIR="$HERE/build-windows"
CACHE_DIR="$HERE/.build-windows-cache"
DIST_DIR="$HERE/dist-windows"
WINEPREFIX="$BUILD_DIR/wineprefix"
mkdir -p "$BUILD_DIR" "$CACHE_DIR" "$DIST_DIR"

# ============================================================================
# Часть 1. Системные зависимости (wine, msitools/wixl, nsis) — Debian/Ubuntu
# ============================================================================

DISTRO_ID=""
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO_ID="${ID:-}"
fi

if [ "$SKIP_DEPS" -eq 0 ]; then
    missing=()
    command -v wine64 >/dev/null 2>&1 || command -v wine >/dev/null 2>&1 || missing+=(wine64)
    command -v wixl >/dev/null 2>&1 || missing+=(wixl)
    command -v makensis >/dev/null 2>&1 || missing+=(nsis)

    if [ "${#missing[@]}" -gt 0 ]; then
        case "$DISTRO_ID" in
            debian|ubuntu|linuxmint|pop)
                echo "Не хватает пакетов: ${missing[*]}"
                do_install=1
                if [ "$ASSUME_YES" -eq 0 ]; then
                    read -r -p "Установить через apt-get? [y/N] " answer
                    case "$answer" in y|Y|yes|Yes|YES) do_install=1 ;; *) do_install=0 ;; esac
                fi
                if [ "$do_install" -eq 1 ]; then
                    SUDO=""
                    [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"
                    $SUDO dpkg --add-architecture i386 || true
                    $SUDO apt-get update
                    # ПРИМЕЧАНИЕ: на Debian/Ubuntu 'wixl' — ОТДЕЛЬНЫЙ пакет
                    # от 'msitools' (сам msitools содержит только
                    # msibuild/msiinfo/msidump/msiextract, без wixl) —
                    # проверено эмпирически. 'nsis' тянет makensis +
                    # стандартные .nsh-инклюды (в т.ч. MUI2.nsh, который
                    # используется ниже в Часть 5).
                    $SUDO apt-get install -y wine wine64 wine32:i386 wixl wixl-data winbind nsis nsis-common || \
                        $SUDO apt-get install -y wine64 wixl wixl-data winbind nsis
                fi
                ;;
            *)
                echo "ПРЕДУПРЕЖДЕНИЕ: автоустановка зависимостей поддержана только для" >&2
                echo "Debian/Ubuntu. Поставьте вручную: wine (64-бит), msitools (wixl) и" >&2
                echo "nsis (makensis), затем запустите заново с --skip-deps." >&2
                ;;
        esac
    fi
fi

if [ "$DEPS_ONLY" -eq 1 ]; then
    echo "Готово (--deps-only): сборка не запускалась."
    exit 0
fi

if [ "$SKIP_MSI" -eq 0 ]; then
    command -v wixl >/dev/null 2>&1 || { echo "ОШИБКА: wixl (пакет 'wixl', отдельный от 'msitools') не найден." >&2; exit 1; }
fi
if [ "$SKIP_EXE_INSTALLER" -eq 0 ]; then
    command -v makensis >/dev/null 2>&1 || { echo "ОШИБКА: makensis (пакет 'nsis') не найден." >&2; exit 1; }
fi
if [ "$SKIP_PORTABLE" -eq 0 ]; then
    WINE_BIN="$(command -v wine64 || command -v wine || true)"
    [ -n "$WINE_BIN" ] || { echo "ОШИБКА: wine не найден." >&2; exit 1; }
fi

# ============================================================================
# Часть 2. Переносимый Windows Python внутри Wine (только если нужно
# пересобирать portable .exe)
# ============================================================================

if [ "$SKIP_PORTABLE" -eq 0 ]; then
    export WINEPREFIX
    export WINEARCH=win64
    # Подавляем всплывающие окна установки Gecko/Mono (не нужны для
    # tkinter-приложения; без этой переменной wine при первой
    # инициализации префикса попытается их скачать и повиснет на
    # диалоге в headless-окружении).
    export WINEDLLOVERRIDES="mscoree,mshtml="

    if [ ! -f "$WINEPREFIX/.initialized" ]; then
        echo "Инициализирую Wine-префикс (один раз)..."
        # ПРИМЕЧАНИЕ: на этом шаге Wine почти всегда печатает "Приложение
        # не может быть запущено... ShellExecuteEx провалился: файл не
        # найден" — известная безобидная особенность wineboot --init
        # (пытается выполнить shell-ассоциацию для стандартного
        # автозапускаемого пункта, которого в свежем префиксе ещё нет) —
        # сборка после этого продолжается нормально.
        "$WINE_BIN" wineboot --init >/dev/null 2>&1 || true
        # timeout — на случай, если "wineserver -w" всё же зависнет
        # (например, DISPLAY указывает на несуществующий X-сервер).
        timeout 30 "$WINE_BIN" wineserver -w >/dev/null 2>&1 || true
        touch "$WINEPREFIX/.initialized"
        echo "Wine-префикс готов."
    fi

    PYTHON_STANDALONE_VERSION="20240415"
    PYTHON_VERSION="3.11.9"
    PY_ARCHIVE="cpython-${PYTHON_VERSION}+${PYTHON_STANDALONE_VERSION}-x86_64-pc-windows-msvc-shared-install_only.tar.gz"
    PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_STANDALONE_VERSION}/${PY_ARCHIVE}"
    PY_CACHE_ARCHIVE="$CACHE_DIR/$PY_ARCHIVE"
    WINPY_DIR="$BUILD_DIR/winpython"

    if [ ! -x "$WINPY_DIR/python.exe" ]; then
        if [ ! -f "$PY_CACHE_ARCHIVE" ]; then
            echo "Скачиваю переносимый Windows Python ${PYTHON_VERSION}..."
            if command -v curl >/dev/null 2>&1; then
                curl -L -o "$PY_CACHE_ARCHIVE" "$PY_URL"
            else
                wget -O "$PY_CACHE_ARCHIVE" "$PY_URL"
            fi
        fi
        echo "Распаковываю Windows Python..."
        rm -rf "$BUILD_DIR/python-extract"
        mkdir -p "$BUILD_DIR/python-extract"
        tar -xzf "$PY_CACHE_ARCHIVE" -C "$BUILD_DIR/python-extract"
        rm -rf "$WINPY_DIR"
        mv "$BUILD_DIR/python-extract/python" "$WINPY_DIR"
        rm -rf "$BUILD_DIR/python-extract"
    fi
    echo "Windows Python готов: $WINPY_DIR/python.exe"

    WINE_PY() { "$WINE_BIN" "$WINPY_DIR/python.exe" "$@"; }

    echo "Устанавливаю/обновляю зависимости внутри Wine-Python (pip)..."
    WINE_PY -m ensurepip --upgrade >/dev/null 2>&1 || true
    WINE_PY -m pip install --upgrade pip wheel >/dev/null
    # Рантайм-зависимостей у программы нет (WASAPI-захват — чистый ctypes,
    # см. win_audio_loopback.py); pyinstaller — сборщик exe.
    WINE_PY -m pip install --upgrade pyinstaller
    echo "Зависимости установлены."

    # ========================================================================
    # Часть 3. Сборка portable .exe через PyInstaller
    # ========================================================================

    echo "Собираю ${APP_NAME}-portable.exe (PyInstaller --onefile)..."
    rm -rf "$BUILD_DIR/pyinstaller-work" "$BUILD_DIR/pyinstaller-dist"

    # ВАЖНО: сборка windowed (--noconsole) — у программы НЕТ окна консоли.
    # Раньше стояло «сознательно без --windowed», из-за чего за окном GUI всегда
    # висело второе, пустое чёрное окно консоли. Спрятать его постфактум нельзя
    # надёжно: PyInstaller --hide-console не срабатывает на Windows 11 с Windows
    # Terminal (pyinstaller issue #8022). Поэтому — windowed, а то, что раньше
    # держалось на консоли, переделано (см. win_stdio.py):
    #   - вывод воркера идёт в лог GUI через pipe (stdin/stdout/stderr передаются
    #     явно; если у воркера sys.stdout оказался None — подставляется рабочий
    #     поток, см. win_stdio.ensure_std_streams);
    #   - подпроцессы (geckodriver, ffmpeg, taskkill) запускаются с
    #     CREATE_NO_WINDOW, иначе каждый открыл бы своё окно консоли;
    #   - «Остановить» = строка STOP в stdin воркера (CTRL_BREAK_EVENT требует
    #     общей консоли, а её больше нет).
    #
    WINE_PY -m PyInstaller \
        --name "$APP_NAME" \
        --onefile \
        --noconsole \
        --distpath "$BUILD_DIR/pyinstaller-dist" \
        --workpath "$BUILD_DIR/pyinstaller-work" \
        --specpath "$BUILD_DIR" \
        --collect-all tkinter \
        --hidden-import auto_record_suno \
        --hidden-import auto_record_youtube \
        --hidden-import screen_capture \
        --hidden-import win_audio_loopback \
        --hidden-import win_audio_devices \
        --hidden-import win_process \
        --hidden-import win_stdio \
        --hidden-import encode_helpers \
        --hidden-import format_options \
        --hidden-import gui_profiles \
        --hidden-import i18n \
        recorder_gui.py

    cp "$BUILD_DIR/pyinstaller-dist/${APP_NAME}.exe" "$DIST_DIR/${APP_NAME}-portable.exe"
    echo "Готово: $DIST_DIR/${APP_NAME}-portable.exe"
else
    echo "Пропускаю сборку portable .exe (--installers-only)."
fi

if [ "$SKIP_MSI" -eq 1 ] && [ "$SKIP_EXE_INSTALLER" -eq 1 ]; then
    echo "Готово (--portable-only): инсталляторы не собирались."
    exit 0
fi

if [ ! -f "$DIST_DIR/${APP_NAME}-portable.exe" ]; then
    echo "ОШИБКА: не найден $DIST_DIR/${APP_NAME}-portable.exe — сначала соберите" >&2
    echo "его (без --installers-only)." >&2
    exit 1
fi

# ============================================================================
# Часть 4. Общая версия сборки для обоих инсталляторов
# ============================================================================

# Автоинкрементный номер сборки — общий для .msi и .exe-инсталлятора,
# хранится в CACHE_DIR (переживает между запусками скрипта), оборачивается
# по модулю 65536 — Windows Installer учитывает в ProductVersion только 3
# поля (major.minor.build), где build ограничен диапазоном 0-65535.
BUILD_NUMBER_FILE="$CACHE_DIR/.installer_build_number"
BUILD_NUMBER=0
if [ -f "$BUILD_NUMBER_FILE" ]; then
    BUILD_NUMBER="$(cat "$BUILD_NUMBER_FILE" 2>/dev/null || echo 0)"
fi
BUILD_NUMBER=$(( (BUILD_NUMBER + 1) % 65536 ))
echo "$BUILD_NUMBER" > "$BUILD_NUMBER_FILE"
APP_VERSION="${APP_VERSION_BASE}.${BUILD_NUMBER}"
echo "Версия инсталляторов: ${APP_VERSION} (автоинкремент номера сборки — см. комментарий у APP_VERSION_BASE)"

# ============================================================================
# Часть 5. Сборка .msi через wixl (msitools)
# ============================================================================

if [ "$SKIP_MSI" -eq 0 ]; then
    echo "Собираю ${APP_NAME}-Setup.msi (wixl)..."

    WXS_FILE="$BUILD_DIR/${APP_NAME}.wxs"
    cat > "$WXS_FILE" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<Wix xmlns="http://schemas.microsoft.com/wix/2006/wi">
  <Product Id="*"
           Name="${APP_DISPLAY_NAME}"
           Language="1033"
           Version="${APP_VERSION}"
           Manufacturer="${APP_PUBLISHER}"
           UpgradeCode="${PRODUCT_UPGRADE_CODE}">
    <Package InstallerVersion="500" Compressed="yes" InstallScope="perMachine" />

    <!-- При установке новой версии поверх старой (тот же UpgradeCode) —
         сначала тихо снести предыдущую, а не завершиться ошибкой
         "уже установлено". -->
    <MajorUpgrade DowngradeErrorMessage="Уже установлена более новая версия ${APP_NAME}." />

    <Media Id="1" Cabinet="app.cab" EmbedCab="yes" />

    <Directory Id="TARGETDIR" Name="SourceDir">
      <Directory Id="ProgramFilesFolder">
        <Directory Id="INSTALLFOLDER" Name="${APP_NAME}">
          <Component Id="MainExecutable" Guid="*">
            <File Id="AppEXE" Source="${DIST_DIR}/${APP_NAME}-portable.exe"
                  Name="${APP_NAME}.exe" KeyPath="yes" />
          </Component>
        </Directory>
      </Directory>
      <Directory Id="ProgramMenuFolder">
        <Directory Id="AppProgramMenuFolder" Name="${APP_NAME}">
          <Component Id="StartMenuShortcut" Guid="*">
            <Shortcut Id="StartMenuShortcutItem"
                      Name="${APP_NAME}"
                      Description="${APP_DISPLAY_NAME}"
                      Target="[INSTALLFOLDER]${APP_NAME}.exe"
                      WorkingDirectory="INSTALLFOLDER" />
            <RemoveFolder Id="RemoveAppProgramMenuFolder" On="uninstall" />
            <RegistryValue Root="HKCU"
                            Key="Software\\${APP_NAME}"
                            Name="installed"
                            Type="integer"
                            Value="1"
                            KeyPath="yes" />
          </Component>
        </Directory>
      </Directory>
    </Directory>

    <Feature Id="MainFeature" Title="${APP_NAME}" Level="1">
      <ComponentRef Id="MainExecutable" />
      <ComponentRef Id="StartMenuShortcut" />
    </Feature>
  </Product>
</Wix>
EOF

    # -a x64 — ОБЯЗАТЕЛЬНО для 64-битного .exe внутри. Без этого флага
    # wixl по умолчанию собирает x86-пакет НЕЗАВИСИМО от того, что
    # написано в .wxs — самый частый задокументированный источник "wixl
    # собрал .msi без ошибок, но Windows отказывается его ставить".
    wixl -v -a x64 -o "$DIST_DIR/${APP_NAME}-Setup.msi" "$WXS_FILE"
    echo "Готово: $DIST_DIR/${APP_NAME}-Setup.msi"
else
    echo "Пропускаю сборку .msi (--no-msi/--portable-only)."
fi

# ============================================================================
# Часть 6. Сборка .exe-инсталлятора через NSIS (makensis)
# ============================================================================

if [ "$SKIP_EXE_INSTALLER" -eq 0 ]; then
    echo "Собираю ${APP_NAME}-Setup.exe (makensis/NSIS)..."

    # unistall.exe регистрируется в HKLM\...\Uninstall — тот же список
    # "Установка и удаление программ"/"Приложения и возможности", где
    # виден и .msi-пакет (см. Часть 5), но под своим собственным ключом
    # (NSIS_UNINSTALL_KEY) — это НЕЗАВИСИМАЯ от MSI запись, автоматической
    # синхронизации/обновления между .msi и .exe-инсталляцией одного и
    # того же приложения НЕТ (ставить оба одновременно не предполагается,
    # см. пояснение у NSIS_UNINSTALL_KEY выше).
    NSI_FILE="$BUILD_DIR/${APP_NAME}.nsi"
    cat > "$NSI_FILE" <<EOF
; ${APP_NAME}.nsi — сгенерирован build-windows.sh, не редактировать руками
; (правки потеряются при следующей пересборке — меняйте сам build-windows.sh).

!include "MUI2.nsh"

Name "${APP_DISPLAY_NAME}"
OutFile "${DIST_DIR}/${APP_NAME}-Setup.exe"
InstallDir "\$PROGRAMFILES64\\${APP_NAME}"
InstallDirRegKey HKLM "Software\\${NSIS_UNINSTALL_KEY}" "InstallDir"
RequestExecutionLevel admin
Unicode true

VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey "ProductName" "${APP_DISPLAY_NAME}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "CompanyName" "${APP_PUBLISHER}"
VIAddVersionKey "FileVersion" "${APP_VERSION}"

!define MUI_ABORTWARNING

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "\$INSTDIR\\${APP_NAME}.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Запустить ${APP_NAME} сейчас"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "Russian"

Section "Install"
    SetOutPath "\$INSTDIR"
    File "${DIST_DIR}/${APP_NAME}-portable.exe"
    Rename "\$INSTDIR\\${APP_NAME}-portable.exe" "\$INSTDIR\\${APP_NAME}.exe"

    CreateDirectory "\$SMPROGRAMS\\${APP_NAME}"
    CreateShortcut "\$SMPROGRAMS\\${APP_NAME}\\${APP_NAME}.lnk" "\$INSTDIR\\${APP_NAME}.exe"
    CreateShortcut "\$SMPROGRAMS\\${APP_NAME}\\Удалить ${APP_NAME}.lnk" "\$INSTDIR\\Uninstall.exe"

    WriteUninstaller "\$INSTDIR\\Uninstall.exe"

    WriteRegStr HKLM "Software\\${NSIS_UNINSTALL_KEY}" "InstallDir" "\$INSTDIR"

    ; Штатная запись в "Установка и удаление программ" / "Приложения и
    ; возможности" — без неё деинсталлятор существует, но пользователь
    ; не увидит программу в этом списке вовсе.
    WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${NSIS_UNINSTALL_KEY}" "DisplayName" "${APP_DISPLAY_NAME}"
    WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${NSIS_UNINSTALL_KEY}" "UninstallString" '"\$INSTDIR\\Uninstall.exe"'
    WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${NSIS_UNINSTALL_KEY}" "QuietUninstallString" '"\$INSTDIR\\Uninstall.exe" /S'
    WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${NSIS_UNINSTALL_KEY}" "InstallLocation" "\$INSTDIR"
    WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${NSIS_UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
    WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${NSIS_UNINSTALL_KEY}" "Publisher" "${APP_PUBLISHER}"
    WriteRegDWORD HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${NSIS_UNINSTALL_KEY}" "NoModify" 1
    WriteRegDWORD HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${NSIS_UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
    Delete "\$INSTDIR\\${APP_NAME}.exe"
    Delete "\$INSTDIR\\Uninstall.exe"
    RMDir "\$INSTDIR"

    Delete "\$SMPROGRAMS\\${APP_NAME}\\${APP_NAME}.lnk"
    Delete "\$SMPROGRAMS\\${APP_NAME}\\Удалить ${APP_NAME}.lnk"
    RMDir "\$SMPROGRAMS\\${APP_NAME}"

    DeleteRegKey HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${NSIS_UNINSTALL_KEY}"
    DeleteRegKey HKLM "Software\\${NSIS_UNINSTALL_KEY}"
SectionEnd
EOF

    makensis -V2 "$NSI_FILE"
    echo "Готово: $DIST_DIR/${APP_NAME}-Setup.exe"
else
    echo "Пропускаю сборку exe-инсталлятора (--no-exe-installer/--portable-only)."
fi

echo
echo "==================================================================="
echo "Собрано в: $DIST_DIR"
[ -f "$DIST_DIR/${APP_NAME}-portable.exe" ] && echo "  - ${APP_NAME}-portable.exe  (portable-версия, ничего не устанавливает)"
[ -f "$DIST_DIR/${APP_NAME}-Setup.msi" ]    && echo "  - ${APP_NAME}-Setup.msi     (Windows Installer, для silent/GPO-разворачивания)"
[ -f "$DIST_DIR/${APP_NAME}-Setup.exe" ]    && echo "  - ${APP_NAME}-Setup.exe     (обычный exe-инсталлятор, wizard 'Далее->Готово')"
echo
echo "ffmpeg и geckodriver НЕ включены в сборку — программа умеет сама"
echo "скачать их при первом запуске (см. README_WINDOWS.md), если не"
echo "найдены в PATH. Чтобы отключить автозагрузку ffmpeg — флаг"
echo "--no-auto-ffmpeg."
echo
echo "НАПОМИНАНИЕ: часть Windows-кода (особенно захват звука через WASAPI,"
echo "win_audio_loopback.py) не проверялась на реальной Windows — прогоните"
echo "хотя бы один полный цикл записи перед боевым использованием."
echo "==================================================================="
