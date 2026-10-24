
from PIL import Image, ImageDraw
from app import calculate_signature_hash, compare_signature_hashes

# These are synthetic patterns for software testing only.
def create_test_image(filename, points):
    image = Image.new("RGB", (320, 120), "white")
    draw = ImageDraw.Draw(image)
    draw.line(points, fill="black", width=5, joint="curve")
    image.save(filename)


# Synthetic pattern A
create_test_image(
    "uploads/synthetic_test_a.png",
    [(15, 85), (40, 25), (65, 80), (95, 35),
     (125, 85), (160, 25), (195, 80), (230, 35), (300, 75)]
)

# A visibly different synthetic pattern B
create_test_image(
    "uploads/synthetic_test_b.png",
    [(15, 30), (45, 90), (75, 25), (105, 95),
     (140, 30), (175, 90), (210, 25), (250, 95), (300, 30)]
)

hash_a = calculate_signature_hash("uploads/synthetic_test_a.png")
hash_b = calculate_signature_hash("uploads/synthetic_test_b.png")

same_image_score = compare_signature_hashes(hash_a, hash_a)
different_image_score = compare_signature_hashes(hash_a, hash_b)

print("Synthetic test images created.")
print("Same-image similarity:", same_image_score, "%")
print("Different-pattern similarity:", different_image_score, "%")
print("No MongoDB records were changed.")
``