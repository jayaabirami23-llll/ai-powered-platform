
from app import check_duplicate_signature

result = check_duplicate_signature(
    "uploads/synthetic_test_a.png"
)

print("Duplicate-check function test")
print("Status:", result.get("status"))
print("Possible duplicate:", result.get("possible_duplicate"))
print("Similarity score:", result.get("similarity_score"))
print("Matched reference:", result.get("matched_reference_id"))
print("Message:", result.get("message"))
