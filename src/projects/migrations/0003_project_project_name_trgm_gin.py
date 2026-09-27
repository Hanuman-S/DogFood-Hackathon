"""A trigram index on the project name, for the gallery's partial-word search.

`projects.search.search_projects` matches a visitor's term two ways: `websearch_to_tsquery` against
the `search_vector`, which matches whole words, and `name__icontains`, which catches the three
letters somebody actually typed. The second is a `LIKE '%...%'` and so unindexable by a B-tree;
pg_trgm is what makes it an index scan instead of a sequential one.

The index is on `UPPER(name)` because that is exactly what Django renders `icontains` as on
Postgres -- an index on the bare column would not be used by that query.
"""

import django.contrib.postgres.indexes
import django.db.models.functions.text
from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0001_initial'),
        ('projects', '0002_project_thumbnail_content_type_and_more'),
        ('teams', '0001_initial'),
    ]

    operations = [
        # Creating an extension needs elevated rights. The compose Postgres runs migrations as its
        # superuser, so this is fine for the documented deployment; a self-hoster on a managed
        # database may have to create the extension by hand first, which the README notes.
        # `BtreeGistExtension` in events/0001 sets the same precedent.
        TrigramExtension(),
        migrations.AddIndex(
            model_name='project',
            index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper('name'), name='gin_trgm_ops'), name='project_name_trgm_gin'),
        ),
    ]
