# Generated by Django 4.2.18 on 2025-03-26 08:08

from django.db import migrations, models


def set_default_file_extensions(apps, schema_editor):
    Settings = apps.get_model('core', 'Settings')
    code_file_extensions = {
        # General purpose languages
        '.py', '.pyi', '.pyx',  # Python
        '.js', '.jsx', '.mjs',  # JavaScript
        '.ts', '.tsx',          # TypeScript
        '.rb', '.rake', '.erb', # Ruby
        '.php',                 # PHP
        '.java',                # Java
        '.scala',               # Scala
        '.kt', '.kts',          # Kotlin
        '.go', '.mod',          # Go
        '.rs',                  # Rust
        '.cpp', '.cc', '.cxx',  # C++
        '.hpp', '.hh', '.hxx',  
        '.c', '.h',            # C
        '.cs',                  # C#
        '.fs', '.fsx',         # F#
        '.swift',              # Swift
        '.m', '.mm',           # Objective-C
        
        # Web technologies
        '.html', '.htm',       # HTML
        '.css', '.scss', '.sass', '.less',  # Stylesheets
        '.vue', '.svelte',     # Web frameworks
        
        # Shell and scripting
        '.sh', '.bash', '.zsh',  # Shell scripts
        '.ps1', '.psm1', '.psd1',  # PowerShell
        '.pl', '.pm',          # Perl
        '.lua',                # Lua
        
        # Functional languages
        '.hs', '.lhs',         # Haskell
        '.ex', '.exs',         # Elixir
        '.erl', '.hrl',        # Erlang
        '.clj', '.cljs',       # Clojure
        
        # Other languages
        '.r', '.R',            # R
        '.dart',               # Dart
        '.groovy',             # Groovy
        '.ml', '.mli',         # OCaml
        '.sol',                # Solidity
        '.cob', '.cbl',        # COBOL
        '.proto',              # Protocol Buffers

        # XML and related formats
        '.xml', '.xsd', '.xslt', '.xsl', '.rss', '.atom', '.svg', '.pom', '.config', '.resx', '.nuspec',
        
        # Markdown and documentation
        '.md', '.mdx', '.markdown', '.rst', '.adoc', '.asciidoc', '.txt', '.text',
        
        # JSON and related formats
        '.json', '.json5', '.jsonc', '.jsonl', '.jsonnet', '.hjson', '.yaml', '.yml', '.yaml-tmlanguage',
    }

    package_manifest_files = {
        # Python
        'requirements.txt',
        'setup.py',
        'pyproject.toml',
        'Pipfile',
        'poetry.toml',
        
        # JavaScript/TypeScript
        'package.json',
        'bower.json',
        
        # Ruby
        'Gemfile',
        
        # Java/Kotlin
        'pom.xml',
        'build.gradle',
        'build.gradle.kts',
        
        # Go
        'go.mod',
        
        # Rust
        'Cargo.toml',
        
        # PHP
        'composer.json',
        
        # .NET/C#
        '*.csproj',
        '*.fsproj',
        'packages.config',
        
        # Swift
        'Package.swift',
        
        # Scala
        'build.sbt',
        
        # Haskell
        'package.yaml',
        'cabal.project',
        
        # Elixir
        'mix.exs',
        
        # R
        'DESCRIPTION',
        
        # Perl
        'cpanfile',
        'Makefile.PL',
    }

    # Update all existing settings
    Settings.objects.all().update(
        code_file_extensions=list(code_file_extensions),
        package_manifest_files=list(package_manifest_files)
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0065_gurucreationform_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='settings',
            name='code_file_extensions',
            field=models.JSONField(blank=True, default=list, null=True),
        ),
        migrations.AddField(
            model_name='settings',
            name='package_manifest_files',
            field=models.JSONField(blank=True, default=list, null=True),
        ),
        migrations.RunPython(set_default_file_extensions),
    ]
