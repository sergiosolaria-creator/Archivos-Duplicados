#!/usr/bin/env python3
"""Encuentra fotos y videos duplicados y opcionalmente mueve los sobrantes."""

import argparse
import hashlib
import os
import shutil
import sys
from collections import defaultdict
from io import BytesIO

import imagehash
from PIL import Image

DEFAULT_MOVE_DESTINATION = "/Volumes/Externo/FotosDuplicadas"
CHUNK_SIZE = 8 * 1024 * 1024

#IMAGE_EXTENSIONS = {
#    ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".gif", ".webp", ".heic",
#}
#VIDEO_EXTENSIONS = {
#    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v", ".3gp",
#    ".mpeg", ".mpg", ".mts", ".m2ts",
#}
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".bmp",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mov",
}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def is_image(path):
    return os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS


def is_video(path):
    return os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS


def should_skip_path(path, excluded_roots):
    normalized = os.path.abspath(path)
    for excluded in excluded_roots:
        if normalized == excluded or normalized.startswith(excluded + os.sep):
            return True
    return False


def iter_media(folder_path, excluded_roots=()):
    """Recorre recursivamente una carpeta y devuelve rutas de fotos y videos."""
    for root, _, filenames in os.walk(folder_path):
        if should_skip_path(root, excluded_roots):
            continue
        for filename in filenames:
            file_path = os.path.join(root, filename)
            if os.path.splitext(filename)[1].lower() in MEDIA_EXTENSIONS:
                if not should_skip_path(file_path, excluded_roots):
                    yield file_path


def hash_file(file_path):
    """Calcula MD5 leyendo el archivo en bloques (eficiente para videos grandes)."""
    digest = hashlib.md5()
    with open(file_path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def analyze_image(file_path, exact_hash, file_size):
    with open(file_path, "rb") as handle:
        data = handle.read()

    with Image.open(BytesIO(data)) as img:
        dimensions = img.size
        rgb = img if img.mode == "RGB" else img.convert("RGB")
        perceptual_hash = str(imagehash.average_hash(rgb))

    return {
        "path": file_path,
        "media_type": "image",
        "exact_hash": exact_hash,
        "perceptual_hash": perceptual_hash,
        "dimensions": dimensions,
        "file_size": file_size,
    }


def analyze_video(file_path, exact_hash, file_size):
    return {
        "path": file_path,
        "media_type": "video",
        "exact_hash": exact_hash,
        "perceptual_hash": None,
        "dimensions": None,
        "file_size": file_size,
    }


def analyze_media(file_path):
    """Analiza una foto o video: hash exacto y, para fotos, hash perceptual."""
    try:
        file_size = os.path.getsize(file_path)
        exact_hash = hash_file(file_path)

        if is_image(file_path):
            return analyze_image(file_path, exact_hash, file_size)
        return analyze_video(file_path, exact_hash, file_size)
    except Exception as exc:
        print(f"Error procesando {file_path}: {exc}", file=sys.stderr)
        return None


def group_by_key(records, key):
    groups = defaultdict(list)
    for record in records:
        groups[record[key]].append(record)

    duplicate_groups = []
    for key_value, members in groups.items():
        if len(members) > 1:
            duplicate_groups.append({
                "key": key_value,
                "files": [member["path"] for member in members],
                "records": members,
                "count": len(members),
            })

    duplicate_groups.sort(key=lambda group: (-group["count"], group["files"][0]))
    return duplicate_groups


def find_visual_duplicates(records):
    image_records = [record for record in records if record["media_type"] == "image"]
    perceptual_groups = group_by_key(image_records, "perceptual_hash")
    visual_groups = []

    for group in perceptual_groups:
        distinct_exact_hashes = {record["exact_hash"] for record in group["records"]}
        if len(distinct_exact_hashes) > 1:
            visual_groups.append({
                "hash": group["key"],
                "files": group["files"],
                "records": group["records"],
                "count": group["count"],
            })

    return visual_groups


def format_bytes(num_bytes):
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


def wasted_bytes_for_exact_group(group):
    records = group["records"]
    if len(records) <= 1:
        return 0
    largest = max(record["file_size"] for record in records)
    return sum(record["file_size"] for record in records) - largest


def choose_file_to_keep(records, move_destination):
    """Elige qué copia conservar en su ubicación original."""
    dest = os.path.abspath(move_destination)

    def sort_key(record):
        path = os.path.abspath(record["path"])
        in_destination = path == dest or path.startswith(dest + os.sep)
        return (in_destination, len(path), path)

    return min(records, key=sort_key)


def unique_destination_path(destination_dir, source_path):
    basename = os.path.basename(source_path)
    destination = os.path.join(destination_dir, basename)
    if not os.path.exists(destination):
        return destination

    stem, ext = os.path.splitext(basename)
    counter = 1
    while True:
        candidate = os.path.join(destination_dir, f"{stem}_{counter}{ext}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def move_duplicate_files(groups, move_destination, dry_run=False):
    os.makedirs(move_destination, exist_ok=True)

    moved = []
    skipped = []
    errors = []

    for group in groups:
        keep = choose_file_to_keep(group["records"], move_destination)
        for record in group["records"]:
            source = record["path"]
            if source == keep["path"]:
                continue

            destination = unique_destination_path(move_destination, source)
            if dry_run:
                moved.append((source, destination))
                continue

            try:
                shutil.move(source, destination)
                moved.append((source, destination))
            except Exception as exc:
                errors.append((source, str(exc)))

    return moved, skipped, errors


def print_exact_duplicates(groups):
    print("\n=== DUPLICADOS EXACTOS (MISMO ARCHIVO) ===")
    if not groups:
        print("No se encontraron duplicados exactos.")
        return 0

    total_wasted = 0
    duplicate_files = 0
    image_groups = 0
    video_groups = 0

    for index, group in enumerate(groups, start=1):
        wasted = wasted_bytes_for_exact_group(group)
        total_wasted += wasted
        duplicate_files += group["count"] - 1

        media_types = {record["media_type"] for record in group["records"]}
        if media_types == {"video"}:
            video_groups += 1
            label = "video"
        elif media_types == {"image"}:
            image_groups += 1
            label = "foto"
        else:
            label = "mixto"

        print(f"\nGrupo {index} | MD5: {group['key']} | {label} | Archivos: {group['count']}")
        print(f"Espacio recuperable: {format_bytes(wasted)}")
        for file_path in group["files"]:
            print(f"  - {file_path}")

    print(
        f"\nResumen exactos: {len(groups)} grupos "
        f"({image_groups} fotos, {video_groups} videos), "
        f"{duplicate_files} archivos redundantes, "
        f"{format_bytes(total_wasted)} recuperables"
    )
    return total_wasted


def print_visual_duplicates(groups):
    print("\n=== DUPLICADOS VISUALES DE FOTOS (MISMO CONTENIDO, DISTINTO ARCHIVO) ===")
    if not groups:
        print("No se encontraron duplicados visuales adicionales.")
        return

    for index, group in enumerate(groups, start=1):
        print(f"\nGrupo {index} | Hash perceptual: {group['hash']} | Archivos: {group['count']}")
        for record in group["records"]:
            width, height = record["dimensions"]
            print(
                f"  - {record['path']} "
                f"({width}x{height}, {format_bytes(record['file_size'])}, "
                f"MD5: {record['exact_hash'][:8]}...)"
            )

    print(f"\nResumen visuales: {len(groups)} grupos detectados.")


def find_duplicate_media(folder_path, excluded_roots=()):
    records = []
    media_paths = list(iter_media(folder_path, excluded_roots))
    total = len(media_paths)

    if total == 0:
        return [], [], 0, 0

    for index, file_path in enumerate(media_paths, start=1):
        print(f"Analizando [{index}/{total}]: {file_path}", end="\r", flush=True)
        record = analyze_media(file_path)
        if record:
            records.append(record)

    images = sum(1 for record in records if record["media_type"] == "image")
    videos = sum(1 for record in records if record["media_type"] == "video")
    print(
        f"\nProcesados {len(records)} de {total} archivos "
        f"({images} fotos, {videos} videos)."
    )

    exact_duplicates = group_by_key(records, "exact_hash")
    visual_duplicates = find_visual_duplicates(records)
    return exact_duplicates, visual_duplicates, len(records), total


def parse_args():
    parser = argparse.ArgumentParser(
        description="Encuentra fotos y videos duplicados en una carpeta (búsqueda recursiva)."
    )
    parser.add_argument(
        "folder",
        nargs="?",
        help="Ruta de la carpeta a analizar (si se omite, se pedirá por consola)",
    )
    parser.add_argument(
        "--move",
        action="store_true",
        help="Mueve duplicados exactos a la carpeta de destino, conservando una copia",
    )
    parser.add_argument(
        "--move-destination",
        default=DEFAULT_MOVE_DESTINATION,
        help=f"Carpeta destino para duplicados (default: {DEFAULT_MOVE_DESTINATION})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Muestra qué archivos se moverían sin moverlos realmente",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    folder_path = args.folder or input("Introduce la ruta de la carpeta con fotos/videos: ").strip()

    if not folder_path:
        print("Debes indicar una ruta.", file=sys.stderr)
        sys.exit(1)

    folder_path = os.path.abspath(folder_path)
    move_destination = os.path.abspath(args.move_destination)

    if not os.path.isdir(folder_path):
        print(f"La ruta no existe o no es una carpeta: {folder_path}", file=sys.stderr)
        sys.exit(1)

    if args.move and not args.dry_run and not os.path.isdir(os.path.dirname(move_destination)):
        print(
            f"No se puede acceder al volumen destino: {move_destination}",
            file=sys.stderr,
        )
        sys.exit(1)

    excluded_roots = (move_destination,)
    print(f"Analizando fotos y videos en: {folder_path}")
    if args.move or args.dry_run:
        mode = "simulación" if args.dry_run else "movimiento real"
        print(f"Modo {mode}: duplicados exactos -> {move_destination}")

    exact_duplicates, visual_duplicates, processed, discovered = find_duplicate_media(
        folder_path,
        excluded_roots=excluded_roots,
    )

    print(f"\nArchivos encontrados: {discovered} | Analizados correctamente: {processed}")
    print_exact_duplicates(exact_duplicates)
    print_visual_duplicates(visual_duplicates)

    if args.move or args.dry_run:
        moved, skipped, errors = move_duplicate_files(
            exact_duplicates,
            move_destination,
            dry_run=args.dry_run,
        )

        print("\n=== MOVIMIENTO DE DUPLICADOS EXACTOS ===")
        if not moved:
            print("No había duplicados exactos para mover.")
        else:
            action = "Se moverían" if args.dry_run else "Movidos"
            print(f"{action} {len(moved)} archivos a {move_destination}")
            for source, destination in moved[:20]:
                print(f"  {source}\n    -> {destination}")
            if len(moved) > 20:
                print(f"  ... y {len(moved) - 20} archivos más")

        if errors:
            print(f"\nErrores al mover {len(errors)} archivos:")
            for source, message in errors[:20]:
                print(f"  - {source}: {message}")


if __name__ == "__main__":
    main()
