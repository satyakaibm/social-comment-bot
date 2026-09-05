from app import db


def _print_comment(row) -> None:
    print("-" * 70)
    print(f"Video:   {row['video_title']}")
    print(f"Author:  {row['author']}")
    print(f"Comment: {row['text']}")
    print(f"Draft:   {row['draft_reply']}")


def review_loop() -> None:
    db.init_db()
    with db.connect() as conn:
        rows = db.list_by_status(conn, "pending_review")
        if not rows:
            print("No comments pending review.")
            return

        print(f"{len(rows)} comment(s) pending review.\n")
        for row in rows:
            _print_comment(row)
            choice = input(
                "[a]pprove / [e]dit & approve / [r]eject / [s]kip / [q]uit: "
            ).strip().lower()

            if choice == "a":
                db.update_status(conn, row["comment_id"], "approved")
                print("Approved.\n")
            elif choice == "e":
                new_text = input("New reply text: ").strip()
                if new_text:
                    db.update_status(
                        conn, row["comment_id"], "approved", draft_reply=new_text
                    )
                    print("Approved with edits.\n")
                else:
                    print("Empty reply, skipped.\n")
            elif choice == "r":
                db.update_status(conn, row["comment_id"], "rejected")
                print("Rejected.\n")
            elif choice == "q":
                print("Stopping review.")
                break
            else:
                print("Skipped.\n")


if __name__ == "__main__":
    review_loop()
