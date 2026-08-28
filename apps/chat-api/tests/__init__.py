"""Marks this directory as a package.

Without it, pytest puts both test directories on ``sys.path`` and the two
``conftest.py`` files collide on the bare module name ``conftest`` — whichever
is imported first wins, and every gateway test fails with
``cannot import name 'Seeded' from 'conftest'``, naming the chat service's file.
Being a package makes this one ``tests.conftest``, which is unique.
"""
