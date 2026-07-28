#!/usr/bin/env python3
"""Encuentra fotos y videos duplicados y opcionalmente mueve los sobrantes."""

import argparse
import csv
import hashlib
import logging
import os
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime
from io import BytesIO

import imagehash
from PIL import Image

# Soporte opcional para HEIC/HEIF (fotos de iPhone). Si pillow-heif está
# instalado, PIL podrá decodificarlas y calcularles hash perceptual; si no,
# esos archivos igual se detectan como duplicados exactos por MD5.
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None

DEFAULT_MOVE_DESTINATION = "/Volumes/Externo/FotosDuplicadas"
CHUNK_SIZE = 8 * 1024 * 1024
MANIFEST_FILENAME = "archivos_movidos.csv"

logger = logging.getLogger("dupfinder")


def setup_logging(log_file):
    """Configura logging a consola (INFO) y a un archivo de log (DEBUG)."""
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".gif", ".webp", ".heic",
    ".heif", ".jfif", ".jpe", ".ico", ".ppm", ".pgm", ".pbm", ".dng", ".raw",
    ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf", ".sr2",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v", ".3gp",
    ".3g2", ".mpeg", ".mpg", ".mts", ".m2ts", ".ts", ".vob", ".ogv", ".mxf",
    ".m2v", ".f4v", ".divx",
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
    for root, dirnames, filenames in os.walk(folder_path):
        if should_skip_path(root, excluded_roots):
            dirnames[:] = []  # no descender en carpetas excluidas
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


def analyze_image(file_path, exact_hash, file_size, data):
    dimensions = None
    perceptual_hash = None
    try:
        with Image.open(BytesIO(data)) as img:
            dimensions = img.size
            rgb = img if img.mode == "RGB" else img.convert("RGB")
            perceptual_hash = str(imagehash.phash(rgb))
    except Exception as exc:
        # Formatos que PIL no puede decodificar (algunos HEIC/RAW sin librería
        # extra): se mantiene la detección de duplicados EXACTOS por MD5 y solo
        # se omite el hash perceptual (duplicados visuales).
        logger.debug(f"Sin hash perceptual (no decodificable) {file_path}: {exc}")

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

        if is_image(file_path):
            # Una sola lectura: el mismo buffer sirve para el MD5 y el hash perceptual.
            with open(file_path, "rb") as handle:
                data = handle.read()
            exact_hash = hashlib.md5(data).hexdigest()
            return analyze_image(file_path, exact_hash, file_size, data)

        # Los videos pueden ser muy grandes: se hashea por bloques sin cargarlos en memoria.
        exact_hash = hash_file(file_path)
        return analyze_video(file_path, exact_hash, file_size)
    except Exception as exc:
        logger.error(f"Error procesando {file_path}: {exc}")
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
    image_records = [
        record
        for record in records
        if record["media_type"] == "image" and record["perceptual_hash"] is not None
    ]
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
    """Conserva la PRIMERA copia encontrada (en orden de descubrimiento).

    Los registros llegan en el orden en que se recorrieron los archivos, por lo
    que records[0] es la primera copia hallada. Por seguridad, si alguna copia ya
    estuviera dentro de la carpeta de destino, se prefiere conservar una que esté
    fuera de ella.
    """
    dest = os.path.abspath(move_destination)

    def in_destination(record):
        path = os.path.abspath(record["path"])
        return path == dest or path.startswith(dest + os.sep)

    for record in records:
        if not in_destination(record):
            return record
    return records[0]


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


def write_moved_manifest(move_destination, moved):
    """Registra en la carpeta destino el path de origen de cada archivo movido.

    El manifiesto es un CSV acumulativo (se añaden filas en ejecuciones sucesivas)
    con: fecha, path de origen y path final dentro de la carpeta de duplicados.
    """
    manifest_path = os.path.join(move_destination, MANIFEST_FILENAME)
    is_new = not os.path.exists(manifest_path)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(manifest_path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if is_new:
            writer.writerow(["fecha", "origen", "destino"])
        for source, destination in moved:
            writer.writerow([timestamp, source, destination])

    logger.info(f"Manifiesto de orígenes actualizado ({len(moved)} entradas): {manifest_path}")
    return manifest_path


def move_duplicate_files(groups, move_destination, dry_run=False):
    os.makedirs(move_destination, exist_ok=True)

    moved = []
    skipped = []
    errors = []

    for group in groups:
        keep = choose_file_to_keep(group["records"], move_destination)
        logger.debug(f"Conservando (primera copia): {keep['path']} (MD5: {group['key']})")
        for record in group["records"]:
            source = record["path"]
            if source == keep["path"]:
                continue

            destination = unique_destination_path(move_destination, source)
            if dry_run:
                logger.debug(f"[SIMULACIÓN] Se movería: {source} -> {destination}")
                moved.append((source, destination))
                continue

            try:
                shutil.move(source, destination)
                logger.debug(f"Movido: {source} -> {destination}")
                moved.append((source, destination))
            except Exception as exc:
                logger.error(f"Error moviendo {source}: {exc}")
                errors.append((source, str(exc)))

    # Solo se escribe el manifiesto cuando hubo movimientos reales.
    manifest_path = None
    if moved and not dry_run:
        manifest_path = write_moved_manifest(move_destination, moved)

    return moved, skipped, errors, manifest_path


def print_exact_duplicates(groups):
    logger.info("\n=== DUPLICADOS EXACTOS (MISMO ARCHIVO) ===")
    if not groups:
        logger.info("No se encontraron duplicados exactos.")
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

        logger.info(f"\nGrupo {index} | MD5: {group['key']} | {label} | Archivos: {group['count']}")
        logger.info(f"Espacio recuperable: {format_bytes(wasted)}")
        for file_path in group["files"]:
            logger.info(f"  - {file_path}")

    logger.info(
        f"\nResumen exactos: {len(groups)} grupos "
        f"({image_groups} fotos, {video_groups} videos), "
        f"{duplicate_files} archivos redundantes, "
        f"{format_bytes(total_wasted)} recuperables"
    )
    return total_wasted


def print_visual_duplicates(groups):
    logger.info("\n=== DUPLICADOS VISUALES DE FOTOS (MISMO CONTENIDO, DISTINTO ARCHIVO) ===")
    if not groups:
        logger.info("No se encontraron duplicados visuales adicionales.")
        return

    for index, group in enumerate(groups, start=1):
        logger.info(f"\nGrupo {index} | Hash perceptual: {group['hash']} | Archivos: {group['count']}")
        for record in group["records"]:
            width, height = record["dimensions"]
            logger.info(
                f"  - {record['path']} "
                f"({width}x{height}, {format_bytes(record['file_size'])}, "
                f"MD5: {record['exact_hash'][:8]}...)"
            )

    logger.info(f"\nResumen visuales: {len(groups)} grupos detectados.")


def find_duplicate_media(folder_path, excluded_roots=()):
    records = []
    logger.info("Buscando archivos de fotos y videos...")
    # Orden alfabético estable: "la primera copia encontrada" es determinista.
    media_paths = sorted(iter_media(folder_path, excluded_roots))
    total = len(media_paths)
    logger.info(f"Archivos multimedia encontrados: {total}")

    if total == 0:
        return [], [], 0, 0

    failed = 0
    for index, file_path in enumerate(media_paths, start=1):
        # Contador en vivo en consola (se reescribe en la misma línea).
        sys.stdout.write(f"Analizando [{index}/{total}]: {file_path[:80]}\r")
        sys.stdout.flush()
        logger.debug(f"Analizando [{index}/{total}]: {file_path}")
        record = analyze_media(file_path)
        if record:
            records.append(record)
        else:
            failed += 1

    sys.stdout.write("\r" + " " * 100 + "\r")  # limpia la línea del contador
    images = sum(1 for record in records if record["media_type"] == "image")
    videos = sum(1 for record in records if record["media_type"] == "video")
    logger.info(
        f"Procesados {len(records)} de {total} archivos "
        f"({images} fotos, {videos} videos). Fallidos: {failed}."
    )

    exact_duplicates = group_by_key(records, "exact_hash")
    visual_duplicates = find_visual_duplicates(records)
    logger.debug(
        f"Grupos exactos: {len(exact_duplicates)} | "
        f"Grupos visuales: {len(visual_duplicates)}"
    )
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
    parser.add_argument(
        "--log-file",
        default=None,
        help="Ruta del archivo de log (default: duplicados_AAAAMMDD_HHMMSS.log en el directorio actual)",
    )
    return parser.parse_args()


def print_final_summary(stats):
    """Imprime y registra un resumen completo de todo lo realizado."""
    logger.info("\n" + "=" * 60)
    logger.info("RESUMEN FINAL")
    logger.info("=" * 60)
    logger.info(f"Carpeta analizada:        {stats['folder']}")
    logger.info(f"Archivos encontrados:     {stats['discovered']}")
    logger.info(f"Analizados correctamente: {stats['processed']}")
    logger.info(f"Fallidos:                 {stats['discovered'] - stats['processed']}")
    logger.info(
        f"Duplicados exactos:       {stats['exact_groups']} grupos, "
        f"{stats['redundant_files']} archivos redundantes"
    )
    logger.info(f"Espacio recuperable:      {format_bytes(stats['wasted'])}")
    logger.info(f"Duplicados visuales:      {stats['visual_groups']} grupos")

    if stats["mode"] != "solo-análisis":
        action = "Se moverían" if stats["mode"] == "simulación" else "Movidos"
        logger.info(f"Movimiento ({stats['mode']}): {action} {stats['moved']} archivos")
        logger.info(f"Destino:                  {stats['move_destination']}")
        if stats.get("manifest"):
            logger.info(f"Manifiesto de orígenes:   {stats['manifest']}")
        if stats["move_errors"]:
            logger.info(f"Errores al mover:         {stats['move_errors']}")

    logger.info(f"Tiempo total:             {stats['elapsed']:.1f} s")
    logger.info(f"Log completo:             {stats['log_file']}")
    logger.info("=" * 60)


def main():
    args = parse_args()

    log_file = args.log_file or os.path.abspath(
        datetime.now().strftime("duplicados_%Y%m%d_%H%M%S.log")
    )
    setup_logging(log_file)
    start_time = time.time()

    folder_path = args.folder or input("Introduce la ruta de la carpeta con fotos/videos: ").strip()

    if not folder_path:
        logger.error("Debes indicar una ruta.")
        sys.exit(1)

    folder_path = os.path.abspath(folder_path)
    move_destination = os.path.abspath(args.move_destination)

    if not os.path.isdir(folder_path):
        logger.error(f"La ruta no existe o no es una carpeta: {folder_path}")
        sys.exit(1)

    if args.move and not args.dry_run and not os.path.isdir(os.path.dirname(move_destination)):
        logger.error(f"No se puede acceder al volumen destino: {move_destination}")
        sys.exit(1)

    excluded_roots = (move_destination,)
    logger.info(f"Log de esta ejecución: {log_file}")
    logger.info(f"Analizando fotos y videos en: {folder_path}")
    mode = "solo-análisis"
    if args.move or args.dry_run:
        mode = "simulación" if args.dry_run else "movimiento real"
        logger.info(f"Modo {mode}: duplicados exactos -> {move_destination}")

    exact_duplicates, visual_duplicates, processed, discovered = find_duplicate_media(
        folder_path,
        excluded_roots=excluded_roots,
    )

    logger.info(f"\nArchivos encontrados: {discovered} | Analizados correctamente: {processed}")
    wasted = print_exact_duplicates(exact_duplicates)
    print_visual_duplicates(visual_duplicates)

    redundant_files = sum(group["count"] - 1 for group in exact_duplicates)
    moved_count = 0
    move_errors = 0

    manifest_path = None
    if args.move or args.dry_run:
        moved, skipped, errors, manifest_path = move_duplicate_files(
            exact_duplicates,
            move_destination,
            dry_run=args.dry_run,
        )
        moved_count = len(moved)
        move_errors = len(errors)

        logger.info("\n=== MOVIMIENTO DE DUPLICADOS EXACTOS ===")
        if not moved:
            logger.info("No había duplicados exactos para mover.")
        else:
            action = "Se moverían" if args.dry_run else "Movidos"
            logger.info(f"{action} {len(moved)} archivos a {move_destination}")
            for source, destination in moved[:20]:
                logger.info(f"  {source}\n    -> {destination}")
            if len(moved) > 20:
                logger.info(f"  ... y {len(moved) - 20} archivos más")

        if errors:
            logger.info(f"\nErrores al mover {len(errors)} archivos:")
            for source, message in errors[:20]:
                logger.info(f"  - {source}: {message}")

    print_final_summary({
        "folder": folder_path,
        "discovered": discovered,
        "processed": processed,
        "exact_groups": len(exact_duplicates),
        "redundant_files": redundant_files,
        "wasted": wasted,
        "visual_groups": len(visual_duplicates),
        "mode": mode,
        "moved": moved_count,
        "move_errors": move_errors,
        "move_destination": move_destination,
        "manifest": manifest_path,
        "elapsed": time.time() - start_time,
        "log_file": log_file,
    })


if __name__ == "__main__":
    main()
