"""Narrow image compatibility fixes applied only to service task execution."""

from __future__ import annotations

from functools import wraps


_CN_LOCAL_IMAGE_PADDING = {
    "main_page_full-notice": 6,
    "cafe_button-goto-no1-cafe": 6,
    "cafe_button-goto-no2-cafe": 6,
    "cafe_confirm-invite": 6,
    "normal_task_start-sweep-notice": 6,
    "normal_task_hard-start-sweep-notice": 6,
    "special_task_request-select": 6,
    "create_start-crafting-notice": 6,
    "lesson_purchase-lesson-ticket-menu": 10,
    "lesson_all-locations": 18,
}

# This dialog grows upward with the number of selected products. Its purchase
# question stays centered, while its vertical position depends on that count.
_CN_VARIABLE_IMAGE_AREAS = {
    "shop_purchase-notice1": (490, 130, 790, 470),
}

_CN_SCALED_CRAFTING_BUTTONS = {
    "create_start-phase-1-bright", "create_start-phase-1-grey",
    "create_select-node", "create_start-crafting",
}


def _match_local_template(context, template, area, threshold, rgb_diff, compare_rgb, padding=6):
    """Allow a bounded layout shift around the feature's configured area."""
    import cv2

    screenshot = context.latest_img_array
    ratio = context.ratio
    if screenshot is None or ratio <= 0 or not area:
        return False
    height, width = screenshot.shape[:2]
    left = max(0, int((area[0] - padding) * ratio))
    top = max(0, int((area[1] - padding) * ratio))
    right = min(width, int((area[2] + padding) * ratio))
    bottom = min(height, int((area[3] + padding) * ratio))
    if right <= left or bottom <= top:
        return False
    search = screenshot[top:bottom, left:right]
    size = (round((right - left) / ratio), round((bottom - top) / ratio))
    if size[0] < template.shape[1] or size[1] < template.shape[0]:
        return False
    search = cv2.resize(search, size, interpolation=cv2.INTER_AREA)
    scores = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, location = cv2.minMaxLoc(scores)
    if not score > threshold:  # Preserve compare_image's strict comparison.
        return False
    x, y = location
    candidate = search[y:y + template.shape[0], x:x + template.shape[1]]
    return bool(compare_rgb(template, candidate, rgb_diff=rgb_diff))


def _match_gift_close_anchor(context, rgb_in_range):
    """Keep the gift title fixed while allowing the close glyph to move locally."""
    feature = context.rgb_feature.get("gift")
    if feature is None or len(feature[0]) != 2 or len(feature[1]) != 2:
        return False
    points, limits = feature
    if not rgb_in_range(context, *points[0], *limits[0]):
        return False
    screenshot, ratio = context.latest_img_array, context.ratio
    if screenshot is None or ratio <= 0:
        return False
    height, width = screenshot.shape[:2]
    x, y = points[1]
    radius = 12
    left, top = max(0, int((x - radius) * ratio)), max(0, int((y - radius) * ratio))
    right = min(width, int((x + radius) * ratio) + 1)
    bottom = min(height, int((y + radius) * ratio) + 1)
    pixels = screenshot[top:bottom, left:right]
    r_min, r_max, g_min, g_max, b_min, b_max = limits[1]
    matches = (
        (pixels[:, :, 2] >= r_min) & (pixels[:, :, 2] <= r_max)
        & (pixels[:, :, 1] >= g_min) & (pixels[:, :, 1] <= g_max)
        & (pixels[:, :, 0] >= b_min) & (pixels[:, :, 0] <= b_max)
    )
    return bool(matches.any())


def _match_crafting_button(context, template, area, threshold, rgb_diff, compare_rgb,
                           verify_phase_digit=False):
    """Match the smaller CN crafting labels and verify numbered stage labels."""
    import cv2

    for scale in (0.825, 0.85):
        height, width = template.shape[:2]
        scaled = cv2.resize(template, (round(width * scale), round(height * scale)),
                            interpolation=cv2.INTER_AREA)
        left, top, right, bottom = (round(value * scale) for value in (52, 3, 76, 42))

        def same_button(reference, candidate, rgb_diff):
            if not compare_rgb(reference, candidate, rgb_diff=rgb_diff):
                return False
            if not verify_phase_digit:
                return True
            digit = reference[top:bottom, left:right]
            search = candidate[max(0, top - 2):bottom + 2, max(0, left - 2):right + 2]
            if not digit.size or search.shape[0] < digit.shape[0] or search.shape[1] < digit.shape[1]:
                return False
            score = cv2.minMaxLoc(cv2.matchTemplate(search, digit, cv2.TM_CCOEFF_NORMED))[1]
            return score > max(0.8, threshold)

        if _match_local_template(context, scaled, area, threshold, rgb_diff, same_button):
            return True
    return False


def _install_local_image_compat() -> None:
    from core import image, position

    original = image.compare_image
    if getattr(original, "_baas_popup_compat", False):
        return

    @wraps(original)
    def compare_image(context, name, threshold=0.8, rgb_diff=20):
        matched = original(context, name, threshold=threshold, rgb_diff=rgb_diff)
        if matched or context.identifier != "CN":
            return matched
        if (name not in _CN_LOCAL_IMAGE_PADDING and name not in _CN_VARIABLE_IMAGE_AREAS
                and name not in _CN_SCALED_CRAFTING_BUTTONS):
            return matched
        template = position.image_dic.get("CN", {}).get(name)
        if template is None:
            return False
        if name in _CN_SCALED_CRAFTING_BUTTONS:
            return _match_crafting_button(
                context, template, position.get_area("CN", name), threshold, rgb_diff,
                image.compare_image_rgb, verify_phase_digit=name.startswith("create_start-phase-1-"),
            )
        if name in _CN_VARIABLE_IMAGE_AREAS:
            return _match_local_template(
                context, template, _CN_VARIABLE_IMAGE_AREAS[name], threshold, rgb_diff,
                image.compare_image_rgb, padding=0,
            )
        return _match_local_template(
            context, template, position.get_area("CN", name), threshold, rgb_diff,
            image.compare_image_rgb, padding=_CN_LOCAL_IMAGE_PADDING[name],
        )

    compare_image._baas_popup_compat = True
    image.compare_image = compare_image


def install_popup_image_compat() -> None:
    """Keep task/image imports lazy and leave desktop GUI comparisons untouched."""
    import sys

    from core import color

    _install_local_image_compat()
    original = color.match_rgb_feature
    if getattr(original, "_baas_popup_compat", False):
        return

    @wraps(original)
    def match_rgb_feature(context, name):
        matched = original(context, name)
        if matched or name != "gift" or context.identifier != "CN":
            return matched
        return _match_gift_close_anchor(context, color.rgb_in_range)

    match_rgb_feature._baas_popup_compat = True
    color.match_rgb_feature = match_rgb_feature
    picture = sys.modules.get("core.picture")
    if picture is not None and getattr(picture, "match_rgb_feature", None) is original:
        picture.match_rgb_feature = match_rgb_feature
