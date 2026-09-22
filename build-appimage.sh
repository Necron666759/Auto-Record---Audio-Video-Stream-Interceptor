#!/bin/bash
# build-appimage.sh
#
# 1) Определяет дистрибутив (Debian/Ubuntu/Fedora/Arch/openSUSE/...),
#    проверяет, каких системных пакетов не хватает для работы программы,
#    и предлагает доустановить их через пакетный менеджер дистрибутива.
# 2) Собирает Recorder-x86_64.AppImage из папки AppDir/, которая лежит
#    рядом с этим скриптом (скачивает appimagetool при необходимости).
#
# Использование:
#   ./build-appimage.sh                собрать AppImage (спросив про
#                                       установку недостающих пакетов)
#   ./build-appimage.sh --yes          то же самое, но без вопросов
#                                       (сразу подтверждает установку)
#   ./build-appimage.sh --skip-deps    не трогать пакеты вообще, только
#                                       собрать AppImage
#   ./build-appimage.sh --deps-only    только проверить/поставить
#                                       пакеты, AppImage не собирать
#
# Требуется на этапе установки пакетов: sudo (если скрипт запущен не от
# root) и доступ в интернет. На этапе сборки: интернет (один раз, чтобы
# скачать appimagetool — он кэшируется рядом со скриптом и повторно не
# скачивается) и wget или curl.

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

ASSUME_YES=0
SKIP_DEPS=0
DEPS_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --yes|-y) ASSUME_YES=1 ;;
        --skip-deps) SKIP_DEPS=1 ;;
        --deps-only) DEPS_ONLY=1 ;;
        --help|-h)
            sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Неизвестный флаг: $arg (см. ./build-appimage.sh --help)" >&2
            exit 1
            ;;
    esac
done

# ====================================================================
# Часть 1. Определение дистрибутива и установка недостающих пакетов
# ====================================================================

DISTRO_ID=""
DISTRO_ID_LIKE=""
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO_ID="${ID:-}"
    DISTRO_ID_LIKE="${ID_LIKE:-}"
fi

# Определяем "семейство" дистрибутива по ID и запасному ID_LIKE — так
# производные дистрибутивы (Mint, Pop!_OS, RHEL/CentOS/Alma/Rocky,
# Manjaro/EndeavourOS, openSUSE Leap/Tumbleweed и т.п.) тоже правильно
# распознаются, даже если их точного ID нет в списке ниже.
FAMILY=""
case "$DISTRO_ID" in
    debian) FAMILY="debian" ;;
    ubuntu|linuxmint|pop|neon|elementary|zorin) FAMILY="ubuntu" ;;
    fedora) FAMILY="fedora" ;;
    rhel|centos|almalinux|rocky) FAMILY="rhel" ;;
    arch|manjaro|endeavouros|artix) FAMILY="arch" ;;
    opensuse|opensuse-leap|opensuse-tumbleweed|sles) FAMILY="suse" ;;
esac
if [ -z "$FAMILY" ]; then
    case "$DISTRO_ID_LIKE" in
        *debian*)
            # Явно не Ubuntu-based — безопаснее считать debian-веткой.
            FAMILY="debian" ;;
        *ubuntu*) FAMILY="ubuntu" ;;
        *fedora*|*rhel*) FAMILY="fedora" ;;
        *arch*) FAMILY="arch" ;;
        *suse*) FAMILY="suse" ;;
    esac
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        echo "ПРЕДУПРЕЖДЕНИЕ: не найден sudo, а скрипт запущен не от root —" >&2
        echo "установка пакетов может не сработать." >&2
    fi
fi

# --- Список пакетов по семействам дистрибутивов ---
declare -a PKGS_CORE=()
declare -a PKGS_OPT=()
INSTALL_CMD=""
UPDATE_CMD=""

case "$FAMILY" in
    debian)
        PKGS_CORE=(python3 python3-tk ffmpeg pulseaudio-utils libpulse0 libfuse2)
        PKGS_OPT=(wf-recorder)
        UPDATE_CMD="$SUDO apt-get update"
        INSTALL_CMD="$SUDO apt-get install -y"
        ;;
    ubuntu)
        PKGS_CORE=(python3 python3-tk ffmpeg pulseaudio-utils libpulse0 libfuse2)
        PKGS_OPT=(wf-recorder)
        UPDATE_CMD="$SUDO apt-get update"
        INSTALL_CMD="$SUDO apt-get install -y"
        ;;
    fedora)
        PKGS_CORE=(python3 python3-tkinter ffmpeg pulseaudio-utils fuse fuse-libs)
        PKGS_OPT=(wf-recorder)
        UPDATE_CMD=""
        INSTALL_CMD="$SUDO dnf install -y"
        ;;
    rhel)
        PKGS_CORE=(python3 python3-tkinter ffmpeg pulseaudio-utils fuse fuse-libs)
        PKGS_OPT=(wf-recorder)
        UPDATE_CMD=""
        INSTALL_CMD="$SUDO dnf install -y"
        ;;
    arch)
        PKGS_CORE=(python tk ffmpeg libpulse pulseaudio fuse2)
        PKGS_OPT=(wf-recorder)
        UPDATE_CMD="$SUDO pacman -Sy"
        INSTALL_CMD="$SUDO pacman -S --needed --noconfirm"
        ;;
    suse)
        PKGS_CORE=(python3 python3-tk ffmpeg pulseaudio-utils libpulse0 fuse)
        PKGS_OPT=(wf-recorder)
        UPDATE_CMD=""
        INSTALL_CMD="$SUDO zypper install -y"
        ;;
    *)
        echo "Не удалось определить дистрибутив (ID='${DISTRO_ID}', ID_LIKE='${DISTRO_ID_LIKE}')."
        echo "Автоматическая установка пакетов пропущена — поставьте вручную по"
        echo "списку из ReadME.txt / ReadME_EN.txt, затем запустите со флагом"
        echo "--skip-deps."
        SKIP_DEPS=1
        ;;
esac

if [ "$SKIP_DEPS" -eq 0 ] && [ -n "$FAMILY" ]; then
    echo "Определён дистрибутив: ${DISTRO_ID:-неизвестно} (семейство: $FAMILY)"

    missing_core=()
    command -v python3 >/dev/null 2>&1 || missing_core+=(python3)
    if command -v python3 >/dev/null 2>&1 && ! python3 -c "import tkinter" >/dev/null 2>&1; then
        case "$FAMILY" in
            debian|ubuntu) missing_core+=(python3-tk) ;;
            fedora|rhel)   missing_core+=(python3-tkinter) ;;
            arch)          missing_core+=(tk) ;;
            suse)          missing_core+=(python3-tk) ;;
        esac
    fi
    command -v ffmpeg >/dev/null 2>&1 || missing_core+=(ffmpeg)
    command -v pactl >/dev/null 2>&1 || missing_core+=(pulseaudio-utils-or-pipewire-pulse)
    command -v parec >/dev/null 2>&1 || missing_core+=(pulseaudio-utils-or-pipewire-pulse)

    missing_opt=()
    command -v wf-recorder >/dev/null 2>&1 || missing_opt+=(wf-recorder)

    if [ "${#missing_core[@]}" -eq 0 ] && [ "${#missing_opt[@]}" -eq 0 ]; then
        echo "Все известные зависимости уже установлены — пропускаю установку пакетов."
    else
        echo
        [ "${#missing_core[@]}" -gt 0 ] && echo "Не хватает (обязательно): ${missing_core[*]}"
        [ "${#missing_opt[@]}" -gt 0 ] && echo "Не хватает (опционально, видео на Wayland): ${missing_opt[*]}"
        echo
        echo "Будут установлены следующие пакеты (${FAMILY}): ${PKGS_CORE[*]} ${PKGS_OPT[*]}"
        echo "(ставится весь список пакетов сразу, а не по одному — так проще и"
        echo " устойчивее к разнице между 'не хватает' и 'название пакета'.)"

        do_install=1
        if [ "$ASSUME_YES" -eq 0 ]; then
            read -r -p "Установить эти пакеты через ${INSTALL_CMD%% *}? [y/N] " answer
            case "$answer" in
                y|Y|yes|Yes|YES) do_install=1 ;;
                *) do_install=0 ;;
            esac
        fi

        if [ "$do_install" -eq 1 ]; then
            if [ -n "$UPDATE_CMD" ]; then
                echo "Обновляю списки пакетов..."
                # Не считаем сбой обновления списков фатальным: пакеты из
                # уже имеющегося локального кэша всё равно могут
                # установиться, а если нет — это будет видно по ошибкам
                # ниже, в установке конкретных пакетов.
                if ! $UPDATE_CMD; then
                    echo "ПРЕДУПРЕЖДЕНИЕ: не удалось обновить списки пакетов, пробую" >&2
                    echo "установить как есть (возможно, из уже имеющегося кэша)." >&2
                fi
            fi
            echo "Устанавливаю пакеты..."
            fail=0
            for pkg in "${PKGS_CORE[@]}"; do
                echo "  -> $pkg"
                if ! $INSTALL_CMD "$pkg"; then
                    echo "     ПРЕДУПРЕЖДЕНИЕ: не удалось установить '$pkg'." >&2
                    fail=1
                fi
            done
            for pkg in "${PKGS_OPT[@]}"; do
                echo "  -> $pkg (опционально)"
                $INSTALL_CMD "$pkg" || echo "     (пропущено — не критично для аудио-записи)"
            done

            if [ "$fail" -eq 1 ]; then
                echo
                echo "Некоторые обязательные пакеты установить не удалось (см. выше)." >&2
                echo "Установите их вручную по списку из ReadME.txt / ReadME_EN.txt." >&2
            fi
        else
            echo "Установка пакетов пропущена по вашему выбору."
        fi
    fi
fi

if [ "$DEPS_ONLY" -eq 1 ]; then
    echo "Готово (--deps-only): сборка AppImage пропущена."
    exit 0
fi

# ====================================================================
# Часть 2. Сборка AppImage
# ====================================================================

APPIMAGETOOL="$HERE/appimagetool-x86_64.AppImage"
ARCH="$(uname -m)"

if [ "$ARCH" != "x86_64" ]; then
    echo "ВНИМАНИЕ: обнаружена архитектура '$ARCH', этот скрипт настроен на x86_64."
    echo "Скачайте appimagetool для вашей архитектуры вручную:"
    echo "  https://github.com/AppImage/appimagetool/releases"
    echo "и положите его рядом с этим скриптом под именем appimagetool-x86_64.AppImage"
    echo "(или отредактируйте переменную APPIMAGETOOL/ARCH в начале скрипта)."
fi

if [ ! -x "$APPIMAGETOOL" ]; then
    echo "Скачиваю appimagetool..."
    URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
    if command -v wget >/dev/null 2>&1; then
        wget -O "$APPIMAGETOOL" "$URL"
    elif command -v curl >/dev/null 2>&1; then
        curl -L -o "$APPIMAGETOOL" "$URL"
    else
        echo "ОШИБКА: нужен wget или curl, чтобы скачать appimagetool." >&2
        exit 1
    fi
    chmod +x "$APPIMAGETOOL"
fi

if [ ! -d "$HERE/AppDir" ]; then
    echo "ОШИБКА: не найдена папка AppDir рядом со скриптом." >&2
    exit 1
fi

# КРИТИЧНО: AppRun — точка входа AppImage, рантайм AppImage делает
# execve() именно на этот файл при запуске. Если он попадёт в squashfs
# БЕЗ бита исполняемости (а git/zip/распаковка архива обычно сбрасывают
# +x, если он не был явно установлен и сохранён), итоговый .AppImage
# будет собираться без единой ошибки, но при запуске выдаст ровно
# "execv error: Permission denied" — сам файл AppImage при этом останется
# исполняемым (у него /этот/ бит выставляется ниже, отдельно), так что
# внешне это выглядит как будто permission works, а на самом деле не
# хватает +x у файла ВНУТРИ образа. Проставляем явно и безусловно,
# на каждой сборке, а не полагаемся на то, что бит сохранился в AppDir.
chmod +x "$HERE/AppDir/AppRun"

echo "Собираю AppImage..."
ARCH="$ARCH" "$APPIMAGETOOL" "$HERE/AppDir" "$HERE/Recorder-${ARCH}.AppImage"

chmod +x "$HERE/Recorder-${ARCH}.AppImage"
echo
echo "Готово: $HERE/Recorder-${ARCH}.AppImage"
echo "Запуск: ./Recorder-${ARCH}.AppImage"
