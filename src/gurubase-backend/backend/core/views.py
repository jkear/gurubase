from asgiref.sync import sync_to_async
from rest_framework.test import APIRequestFactory
from slack_sdk.errors import SlackApiError
import json
import logging
import aiohttp
import random
import string
import time
from datetime import UTC, datetime, timedelta
from typing import Generator
import re
from accounts.models import User
from django.conf import settings
from django.core.cache import caches
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from slack_sdk import WebClient
from core.github.exceptions import GithubAppHandlerError
from core.requester import ConfluenceRequester, GeminiRequester, OpenAIRequester, OllamaRequester
from core.data_sources import CrawlService, YouTubeService
from core.serializers import WidgetIdSerializer, BingeSerializer, DataSourceSerializer, GuruTypeSerializer, GuruTypeInternalSerializer, QuestionCopySerializer, FeaturedDataSourceSerializer, APIKeySerializer, DataSourceAPISerializer, SettingsSerializer
from core.auth import auth, follow_up_examples_auth, jwt_auth, combined_auth, stream_combined_auth, api_key_auth
from core.gcp import replace_media_root_with_base_url, replace_media_root_with_nginx_base_url
from core.models import CrawlState, FeaturedDataSource, Question, ContentPageStatistics, Settings, WidgetId, Binge, DataSource, GuruType, Integration, Thread, APIKey, GuruCreationForm
from accounts.models import User
from core.utils import (
    # Authentication & validation
    check_binge_auth, create_fresh_binge, decode_guru_slug, encode_guru_slug, generate_jwt, validate_binge_follow_up,
    validate_guru_type, validate_image, 
    
    # Question & answer handling
    get_question_summary, 
    handle_failed_root_reanswer, is_question_dirty, search_question,
    stream_question_answer, stream_and_save,
    
    # Content formatting & generation
    format_references, format_trust_score, format_date_updated,
    generate_og_image, 
    
    # Data management
    create_binge_helper, create_custom_guru_type_slug,
    create_guru_type_object, upload_image_to_storage,
    
)
from core.guru_types import get_guru_type_object, get_guru_types, get_guru_type_object_by_maintainer, get_auth0_user
from core.exceptions import IntegrityError, PermissionError, NotFoundError
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework import status
from core.integrations.helpers import IntegrationError, cleanup_title, get_trust_score_emoji, strip_first_header, NotEnoughData, NotRelated
from core.integrations.factory import IntegrationFactory
from rest_framework.decorators import api_view, parser_classes, throttle_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from core.auth import (
    api_key_auth,
    auth,
    combined_auth,
    follow_up_examples_auth,
    jwt_auth,
    stream_combined_auth,
    widget_id_auth,
)
from core.exceptions import NotFoundError, PermissionError
from core.gcp import replace_media_root_with_nginx_base_url
from core.guru_types import (
    get_auth0_user,
    get_guru_type_object,
    get_guru_type_object_by_maintainer,
    get_guru_types,
)
from core.handlers.response_handlers import (
    APIResponseHandler,
    DataSourceResponseHandler,
    WidgetResponseHandler,
)
from core.models import (
    APIKey,
    Binge,
    ContentPageStatistics,
    DataSource,
    FeaturedDataSource,
    GuruType,
    Question,
    WidgetId,
    Integration,
)
from core.requester import GeminiRequester, JiraRequester, ZendeskRequester
from core.serializers import (
    APIKeySerializer,
    BingeSerializer,
    DataSourceAPISerializer,
    DataSourceSerializer,
    FeaturedDataSourceSerializer,
    GuruTypeInternalSerializer,
    GuruTypeSerializer,
    QuestionCopySerializer,
    WidgetIdSerializer,
)
from core.services.data_source_service import DataSourceService
from core.throttling import ConcurrencyThrottleApiKey
from core.utils import (
    APIAskResponse,
    APIType,
    api_ask,
    check_binge_auth,
    create_binge_helper,
    create_custom_guru_type_slug,
    create_guru_type_object,
    format_date_updated,
    format_references,
    format_trust_score,
    generate_jwt,
    generate_og_image,
    get_question_summary,
    handle_failed_root_reanswer,
    is_question_dirty,
    search_question,
    stream_and_save,
    stream_question_answer,
    upload_image_to_storage,
    validate_binge_follow_up,
    validate_guru_type,
    validate_image,
)
from core.integrations.rest_commands import GetIntegrationCommand, DeleteIntegrationCommand, CreateIntegrationCommand
from .github.event_handler import GitHubEventFactory, GitHubEventHandler
from .github.app_handler import GithubAppHandler


logger = logging.getLogger(__name__)

def conditional_csrf_exempt(view_func):
    """Decorator to conditionally apply csrf_exempt based on settings"""
    if settings.ENV == 'selfhosted':
        return csrf_exempt(view_func)
    else:
        return view_func

@api_view(['POST'])
@combined_auth
def summary(request, guru_type):
    from core.utils import get_default_settings
    times = {
        'payload_processing': 0,
        'existence_check': 0,
        'dirtiness_check': 0,
        'total': 0
    }

    guru_type_object = get_guru_type_object(guru_type)

    if settings.ENV == 'selfhosted':
        default_settings = get_default_settings()
        valid = False
        if default_settings.ai_model_provider == Settings.AIProvider.OPENAI:
            valid = default_settings.is_openai_key_valid
            error_type = 'openai'
            reason = 'openai_key_invalid'
        elif default_settings.ai_model_provider == Settings.AIProvider.OLLAMA:
            valid = default_settings.is_ollama_url_valid and default_settings.is_ollama_base_model_valid and default_settings.is_ollama_embedding_model_valid
            error_type = 'ollama'
            if not default_settings.is_ollama_url_valid:
                reason = 'ollama_url_invalid'
            elif not (default_settings.is_ollama_base_model_valid and default_settings.is_ollama_embedding_model_valid):
                reason = 'ollama_model_invalid'
        if not valid:
            return Response({'msg': 'Invalid AI model provider settings', 'reason': reason, 'type': error_type}, status=490)

    if settings.ENV == 'selfhosted':
        user = None
    else:
        if request.user.is_anonymous:
            user = None
        else:
            user = request.user
    
    endpoint_start = time.time()
    payload_start = time.time()
    try:
        data = request.data
        question = data.get('question')
        binge_id = data.get('binge_id')
        parent_question_slug = data.get('parent_question_slug')
    except Exception as e:
        logger.error(f'Error parsing request data: {e}', exc_info=True)
        question = None
        
    if question is None:
        return Response({'msg': "Please provide a question in the request body"}, status=status.HTTP_400_BAD_REQUEST)

    if binge_id:
        try:
            binge = Binge.objects.get(id=binge_id)
            if not check_binge_auth(binge, request.user):
                return Response({'msg': 'User does not have access to this binge'}, status=status.HTTP_401_UNAUTHORIZED)
        except Binge.DoesNotExist:
            return Response({'msg': 'Binge not found'}, status=status.HTTP_404_NOT_FOUND)
    else:
        binge = None
        
    if parent_question_slug:
        parent_question = search_question(
            user, 
            guru_type_object,
            binge,
            parent_question_slug,
            question
        )
        if not parent_question:
            return Response({'msg': "Parent question not found"}, status=status.HTTP_404_NOT_FOUND)
    else:
        parent_question = None
        
    times['payload_processing'] = time.time() - payload_start

    existence_start = time.time()
    
    if not binge:
        # Only check existence for non-binge. We want to re-answer the binge questions again, and save them as separate questions.
        existing_question = search_question(
            request.user, 
            guru_type_object, 
            binge, 
            None, 
            question
        )
    else:
        existing_question = None
    times['existence_check'] = time.time() - existence_start

    # if existing_question:
    #     dirtiness_start = time.time()
    #     is_dirty = is_question_dirty(existing_question)
    #     times['dirtiness_check'] = time.time() - dirtiness_start
        
    #     if not is_dirty:
    #         response = {
    #             'question': existing_question.question,
    #             'question_slug': existing_question.slug,
    #             'description': existing_question.description,
    #             'user_question': existing_question.user_question,
    #             'valid_question': True,
    #             'completion_tokens': 0,
    #             'prompt_tokens': 0,
    #             'cached_prompt_tokens': 0,
    #             "jwt": generate_jwt(),
    #         }
    #         times['total'] = time.time() - endpoint_start
    #         return Response(response, status=status.HTTP_200_OK)

    answer, get_question_summary_times = get_question_summary(
        question, 
        guru_type, 
        binge, 
        short_answer=False,
        parent_question=parent_question
    )

    times['get_question_summary'] = get_question_summary_times

    # if existing_question:
    #     answer['question_slug'] = existing_question.slug

    times['total'] = time.time() - endpoint_start
    
    if settings.LOG_STREAM_TIMES:
        logger.info(f'Summary times: {times}')

    answer['times'] = times

    return Response(answer, status=status.HTTP_200_OK)


@api_view(['POST'])
@stream_combined_auth
@conditional_csrf_exempt
def answer(request, guru_type):
    endpoint_start = time.time()

    guru_type_object = get_guru_type_object(guru_type)
    
    if settings.ENV == 'selfhosted':
        user = None
    else:
        if request.user.is_anonymous:
            user = None
        else:
            user = request.user


    # jwt_time = time.time() - jwt_start    
    payload_start = time.time()
    try:
        data = request.data
        user_question = data.get('user_question')
        question = data.get('question')
        description = data.get('description')
        question_slug = data.get('question_slug')
        completion_tokens = data.get('completion_tokens')
        cached_prompt_tokens = data.get('cached_prompt_tokens')
        prompt_tokens = data.get('prompt_tokens')
        user_intent = data.get('user_intent')
        answer_length = data.get('answer_length')
        parent_question_slug = data.get('parent_question_slug')
        binge_id = data.get('binge_id')
        source = data.get('source', Question.Source.USER.value)   # RAW_QUESTION, USER, REDDIT, SUMMARY_QUESTION
        summary_times = data.get('times')
        enhanced_question = data.get('enhanced_question')
    except Exception as e:
        logger.error(f'Error parsing request data: {e}', exc_info=True)
        question = None

    if binge_id:
        try:
            binge = Binge.objects.get(id=binge_id)
        except Binge.DoesNotExist:
            return Response({'msg': 'Binge not found'}, status=status.HTTP_404_NOT_FOUND)
    else:
        binge = None
        
    if binge and not check_binge_auth(binge, user):
        return Response({'msg': 'User does not have access to this binge'}, status=status.HTTP_401_UNAUTHORIZED)
        
    if question is None or description is None or question_slug is None:
        return Response({'msg': "Please provide all the required fields (question, description, question_slug) in the request body"}, status=status.HTTP_400_BAD_REQUEST)

    if parent_question_slug:
        parent_question = search_question(
            user, 
            guru_type_object,
            binge,
            parent_question_slug,
            None
        )
        if not parent_question:
            return Response({'msg': "Parent question not found"}, status=status.HTTP_404_NOT_FOUND)
    else:
        parent_question = None
        
    if binge and not check_binge_auth(binge, user):
        return Response({'msg': 'User does not have access to this binge'}, status=status.HTTP_401_UNAUTHORIZED)

    if settings.ENV != 'selfhosted':
        valid, msg = validate_binge_follow_up(parent_question, binge, user)
        if not valid:
            return Response({'msg': msg}, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    # existing_question = check_if_question_exists(question, guru_type)
    # if existing_question:
    #     def stream_data():
    #        # Split the content into chunks. Here we assume each chunk is a line.
    #         content_chunks = existing_question.content.split('\n')
    #         for chunk in content_chunks:
    #             yield f"{chunk}\n"
    #             # Simulate delay between chunks
    #             time.sleep(0.1)
    #     return StreamingHttpResponse(stream_data(),content_type='text/event-stream')

    payload_time = time.time() - payload_start    
    stream_obj_start = time.time()
    try:
        response, prompt, links, context_vals, context_distances, reranked_scores, trust_score, processed_ctx_relevances, ctx_rel_usage, before_stream_times = stream_question_answer(
            question, 
            guru_type, 
            user_intent, 
            answer_length, 
            user_question, 
            source,
            enhanced_question,
            parent_question, 
            user,
        )
        if not response:
            if not binge:  # Only notify root questions
                handle_failed_root_reanswer(question_slug, guru_type, user_question, question)
            return Response({'msg': "Can not answer the question because of the lack of context"}, status=status.HTTP_406_NOT_ACCEPTABLE)
    except Exception as e:
        logger.error(f'Error while getting answer: {e}', exc_info=True)
        return Response({'msg': "An error occurred while getting the answer"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    times = {}
    times['before_stream'] = before_stream_times
    if summary_times:
        times['summary'] = summary_times

    return StreamingHttpResponse(stream_and_save(
        user_question, 
        question, 
        guru_type, 
        question_slug, 
        description, 
        response, 
        prompt, 
        links, 
        completion_tokens, 
        prompt_tokens, 
        cached_prompt_tokens, 
        context_vals, 
        context_distances, 
        times, 
        reranked_scores, 
        trust_score, 
        processed_ctx_relevances,
        ctx_rel_usage,
        enhanced_question,
        user,
        parent_question, 
        binge, 
        source,
    ), content_type='text/event-stream')


@api_view(['GET'])
@combined_auth
def question_detail(request, guru_type, slug):
    # This endpoint is only used for UI.
    guru_type_object = get_guru_type_object(guru_type)
    user = request.user
    
    binge_id = request.query_params.get('binge_id')
    if binge_id:
        try:
            binge = Binge.objects.get(id=binge_id)
        except Binge.DoesNotExist:
            return Response({'msg': 'Binge not found'}, status=status.HTTP_404_NOT_FOUND)
    else:
        binge = None

    if binge and not check_binge_auth(binge, user):
        return Response({'msg': 'User does not have access to this binge'}, status=status.HTTP_401_UNAUTHORIZED)

    
    question = search_question(
        user, 
        guru_type_object, 
        binge, 
        slug, 
        None, # Do not search questions by question text, as we want to ask the same question again. This is not the case for integrations or API, but only for UI.
        allow_maintainer_access=True
    )
    if not question:
        return Response({'msg': 'Question does not exist'}, status=status.HTTP_404_NOT_FOUND)

    similar_questions = question.similar_questions

    question_data = {
        'slug': question.slug,
        'parent_slug': question.parent.slug if question.parent else None,
        'question': question.question,
        'content': question.content,
        'description': question.description,
        'references': format_references(question.references),
        'noindex': not question.add_to_sitemap,
        'trust_score': format_trust_score(question.trust_score),
        'similar_questions': similar_questions,
        'og_image_url': question.og_image_url,
        'dirty': is_question_dirty(question),
        'date_updated': format_date_updated(question.date_updated),
        'date_created_meta': question.date_created,
        'date_updated_meta': question.date_updated,
        'follow_up_questions': question.follow_up_questions,
        'source': question.source
    }

    return Response(question_data)


@api_view(['GET'])
@auth
def guru_types(request):
    # Default get all active guru types. If ?all=1, get all guru types.
    get_all = request.query_params.get('all', '0')
    if get_all == '1':
        return Response(get_guru_types(only_active=False), status=status.HTTP_200_OK)
    else:
        return Response(get_guru_types(only_active=True), status=status.HTTP_200_OK)

@api_view(['GET'])
@auth
def guru_type(request, slug):
    guru_type_object = get_guru_type_object(slug)
    serializer = GuruTypeInternalSerializer(guru_type_object)
    return Response(serializer.data, status=status.HTTP_200_OK)

@api_view(['GET'])
@jwt_auth
def my_gurus(request, guru_slug=None):
    try:
        if settings.ENV == 'selfhosted':
            user = None
        else:
            user = User.objects.get(auth0_id=request.auth0_id)

        if guru_slug:
            sub_query = GuruType.objects.filter(slug=guru_slug)
        else:
            sub_query = GuruType.objects.all()

        if sub_query.count() == 0 and guru_slug:
            return Response({'msg': 'Guru not found'}, status=status.HTTP_404_NOT_FOUND)

        if settings.ENV == 'selfhosted' or user.is_admin:
            user_gurus = sub_query.filter(active=True).order_by('-date_created')
        else:
            user_gurus = sub_query.filter(maintainers=user, active=True).order_by('-date_created')
        
        gurus_data = []
        for guru in user_gurus:

            widget_ids = WidgetId.objects.filter(guru_type=guru)

            if settings.ENV == 'selfhosted':
                icon_url = replace_media_root_with_nginx_base_url(guru.icon_url)
            else:
                icon_url = guru.icon_url            

            gurus_data.append({
                'id': guru.id,
                'name': guru.name,
                'slug': guru.slug,
                'icon': icon_url,
                'icon_url': icon_url,
                'domain_knowledge': guru.domain_knowledge,
                'github_repos': guru.github_repos,
                'index_repo': guru.index_repo,
                'youtubeCount': 0,
                'pdfCount': 0,
                'websiteCount': 0,
                'youtube_limit': guru.youtube_count_limit,
                'website_limit': guru.website_count_limit,
                'pdf_size_limit_mb': guru.pdf_size_limit_mb,
                'jira_limit': guru.jira_count_limit,
                'zendesk_limit': guru.zendesk_count_limit,
                'widget_ids': WidgetIdSerializer(widget_ids, many=True).data,
                'github_repo_limit': guru.github_repo_count_limit
            })
        
        if guru_slug:
            return Response(gurus_data[0], status=status.HTTP_200_OK)
        else:
            return Response(gurus_data, status=status.HTTP_200_OK)
    except Exception as e:
        logger.error(f'Error while fetching user gurus: {e}', exc_info=True)
        return Response({'msg': str(e)}, status=500)


@api_view(['GET'])
@auth
def default_questions(request, guru_type):
    try:
        questions = Question.objects.filter(default_question=True, guru_type__slug=guru_type).values('slug', 'question', 'description')
    except Exception as e:
        logger.error(f'Error while fetching default questions: {e}', exc_info=True)
        return Response({'msg': "An error occurred while fetching default questions"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    return Response(questions, status=status.HTTP_200_OK)


@api_view(['POST'])
@auth
def record_page_visit(request, guru_type):
    return Response(status=status.HTTP_200_OK)
    content_slug = request.data.get('content_slug')
    fingerprint = request.data.get('fingerprint')
    
    if not content_slug or not fingerprint:
        return Response({'msg': 'content_slug and fingerprint are required'}, status=status.HTTP_400_BAD_REQUEST)

    validate_guru_type(guru_type)

    cache_key = f"visit:{guru_type}:{content_slug}:{fingerprint}"
    cache = caches['alternate']

    # Check if this fingerprint has visited in the last hour
    if not cache.get(cache_key):
        try:
            question = Question.objects.get(slug=content_slug, guru_type__slug=guru_type)
            stats, created = ContentPageStatistics.objects.get_or_create(question=question)
            stats.view_count += 1
            stats.save()

            # Set cache to expire in 1 hour
            cache.set(cache_key, True, 1 * 60 * 60)
        except Question.DoesNotExist:
            return Response({'msg': 'Question not found'}, status=status.HTTP_404_NOT_FOUND)

    return Response(status=status.HTTP_200_OK)


@api_view(['POST'])
@auth
def record_vote(request, guru_type):

    content_slug = request.data.get('content_slug')
    vote_type = request.data.get('vote_type')  # 'upvote' or 'downvote'
    fingerprint = request.data.get('fingerprint')

    if not content_slug or vote_type not in ['upvote', 'downvote'] or not fingerprint:
        return Response({'msg': 'Invalid parameters'}, status=status.HTTP_400_BAD_REQUEST)

    validate_guru_type(guru_type)

    cache_key = f"vote:{guru_type}:{content_slug}:{fingerprint}"
    cache = caches['alternate']

    # Check if this fingerprint has voted in the last 24 hours
    if not cache.get(cache_key):
        try:
            question = Question.objects.get(slug=content_slug, guru_type__slug=guru_type)
            stats, created = ContentPageStatistics.objects.get_or_create(question=question)
            
            if vote_type == 'upvote':
                stats.upvotes += 1
            elif vote_type == 'downvote':
                stats.downvotes += 1
            else:
                return Response({'msg': 'Invalid vote type'}, status=status.HTTP_400_BAD_REQUEST)
            
            stats.save()

            # Set cache to expire in 24 hours
            cache.set(cache_key, True, 24 * 60 * 60)

            return Response(status=status.HTTP_200_OK)
        except Question.DoesNotExist:
            return Response({'msg': 'Question not found'}, status=status.HTTP_404_NOT_FOUND)
    else:
        return Response(status=status.HTTP_200_OK)

        
@api_view(['GET'])
@auth
def get_processed_raw_questions(request):
    page_num = request.query_params.get('page_num')
    page_size = 100

    if not page_num:
        return Response({'msg': 'Page num must be included'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        page_num = int(page_num)
    except ValueError:
        return Response({'msg': 'Page num must be an integer'}, status=status.HTTP_400_BAD_REQUEST)
    
    raw_questions = Question.objects.filter(source=Question.Source.RAW_QUESTION).order_by('date_created')
    start = (page_num - 1) * page_size
    end = page_num * page_size
    raw_questions = raw_questions[start:end]
    serializer = QuestionCopySerializer(raw_questions, many=True)
    return Response(serializer.data, status=status.HTTP_200_OK)


@api_view(['GET'])
@auth
def og_image_generate(request,question_id):
    url, success = generate_og_image(question_id)
    if success:
        return Response({'msg': "OG Image generated", "url": "not implemented yet"}, status=status.HTTP_200_OK)
    else:
        return Response({'msg': "Error generating OG Image"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@auth
def get_guru_type_resources(request, guru_type):
    validate_guru_type(guru_type)
    try:
        resources = []

        # Add featured data sources first
        featured_data_sources = FeaturedDataSource.objects.filter(guru_type__slug=guru_type, active=True)
        serializer = FeaturedDataSourceSerializer(featured_data_sources, many=True)
        
        for data_source in serializer.data:
            resources.append({
                "title": data_source['title'],
                "description": data_source['description'],
                "icon": data_source['icon_url'],
                "url": data_source['url']
            })

        response_data = {
            'guru_type': guru_type,
            'total_resources': len(resources),
            'resources': resources
        }

        return Response(response_data, status=status.HTTP_200_OK)

    except GuruType.DoesNotExist:
        logger.warning(f'Guru type "{guru_type}" not found')
        return Response({'msg': f'Guru type "{guru_type}" not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f'Error while getting guru type resources: {e}', exc_info=True)
        return Response({'msg': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

def create_guru_type(name, domain_knowledge, intro_text, stackoverflow_tag, stackoverflow_source, github_repos, image, maintainer=None):
    """Utility function to handle guru type creation logic"""
    if not name or len(name) < 2:
        raise ValueError('Guru type name must be at least 2 characters')
    if len(name) > 18:
        raise ValueError('Guru type name must not exceed 18 characters')

    error, split = validate_image(image)
    if error:
        raise ValueError(error)

    name_without_extension = split[0].replace(' ', '_')
    extension = split[1]

    error, icon_url = upload_image_to_storage(image, name_without_extension, extension)
    if error:
        raise ValueError(error)

    try:
        slug = create_custom_guru_type_slug(name)
    except Exception as e:
        logger.error(f'Error while slugifying the name: {e}', exc_info=True)
        raise ValueError(e.args[0])

    if len(slug) < 2:
        raise ValueError('Guru type name must be at least 2 characters')
    
    if GuruType.objects.filter(slug=slug).exists():
        raise ValueError(f'Guru type {slug} already exists')

    try:
        github_repos = json.loads(github_repos)
    except Exception as e:
        logger.error(f'Error while parsing github repos: {e}', exc_info=True)
        raise ValueError('Github repos must be a list of strings')

    try:
        guru_type_object = create_guru_type_object(
            slug, name, intro_text, domain_knowledge, icon_url, 
            stackoverflow_tag, stackoverflow_source, github_repos, maintainer
        )
    except ValidationError as e:
        raise
    except Exception as e:
        logger.error(f'Error while creating guru type: {e}', exc_info=True)
        raise ValueError(e.args[0])
        
    return guru_type_object

@api_view(['POST'])
@auth
def create_guru_type_internal(request):
    data = request.data
    try:
        guru_type_object = create_guru_type(
            name=data.get('name'),
            domain_knowledge=data.get('domain_knowledge'),
            intro_text=data.get('intro_text'),
            stackoverflow_tag=data.get('stackoverflow_tag', ""),
            stackoverflow_source=data.get('stackoverflow_source', False),
            github_repos=data.get('github_repos', ""),
            image=request.FILES.get('icon_image'),
        )
        return Response(GuruTypeSerializer(guru_type_object).data, status=status.HTTP_200_OK)
    except ValueError as e:
        return Response({'msg': str(e)}, status=status.HTTP_400_BAD_REQUEST)

@api_view(['POST'])
@jwt_auth
def create_guru_type_frontend(request):
    try:
        if settings.ENV == 'selfhosted':
            user = None
        else:
            user = get_auth0_user(request.auth0_id)
            if not user.is_admin:
                raise PermissionError(f'User {user.auth0_id} is not an admin')
    except PermissionError:
        return Response({'msg': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    
    data = request.data
    try:
        guru_type_object = create_guru_type(
            name=data.get('name'),
            domain_knowledge=data.get('domain_knowledge'),
            intro_text=data.get('intro_text'),
            stackoverflow_tag=data.get('stackoverflow_tag', ""),
            stackoverflow_source=data.get('stackoverflow_source', False),
            github_repos=data.get('github_repos', ""),
            image=request.FILES.get('icon_image'),
            maintainer=user
        )
        return Response(GuruTypeSerializer(guru_type_object).data, status=status.HTTP_200_OK)
    except ValidationError as e:
        return Response({'msg': str(e.message_dict['msg'][0])}, status=status.HTTP_400_BAD_REQUEST)
    except ValueError as e:
        return Response({'msg': str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@auth
def add_featured_ds_via_api(request, guru_type):
    if not guru_type:
        return Response({'msg': 'Guru type is required'}, status=status.HTTP_400_BAD_REQUEST)
    
    guru_type_object = get_guru_type_object(guru_type, only_active=False)
    
    featured_datasources = json.loads(request.data.get('featured_datasources'))
    for fds in featured_datasources:
        fds_type = fds.get('type')
        if fds_type not in [ds_type[0] for ds_type in DataSource.Type.choices]:
            return Response({'msg': 'Invalid type'}, status=status.HTTP_400_BAD_REQUEST)
        FeaturedDataSource.objects.create(
            guru_type=guru_type_object,
            type=fds_type,
            title=fds.get('title'),
            description=fds.get('description'),
            icon_url=fds.get('icon_url'),
            url=fds.get('url')
        )
    
    return Response({'msg': 'Featured data source added successfully'}, status=status.HTTP_200_OK)
    
    

    
@parser_classes([MultiPartParser, FormParser])
def create_data_sources(request, guru_type):
    guru_type_object = get_guru_type_object(guru_type, only_active=False)

    pdf_files = request.FILES.getlist('pdf_files', [])
    youtube_urls = request.data.get('youtube_urls', '[]')
    website_urls = request.data.get('website_urls', '[]')
    github_urls = request.data.get('github_urls', '[]')
    pdf_privacies = request.data.get('pdf_privacies', '[]')
    jira_urls = request.data.get('jira_urls', '[]')
    zendesk_urls = request.data.get('zendesk_urls', '[]')
    confluence_urls = request.data.get('confluence_urls', '[]')
    try:
        if type(youtube_urls) == str:
            youtube_urls = json.loads(youtube_urls)
        if type(website_urls) == str:
            website_urls = json.loads(website_urls)
        if type(github_urls) == str:
            github_urls = json.loads(github_urls)
        if type(pdf_privacies) == str:
            pdf_privacies = json.loads(pdf_privacies)
        if type(jira_urls) == str:
            jira_urls = json.loads(jira_urls)
        if type(zendesk_urls) == str:
            zendesk_urls = json.loads(zendesk_urls)
        if type(confluence_urls) == str:
            confluence_urls = json.loads(confluence_urls)
    except Exception as e:
        logger.error(f'Error while parsing urls: {e}', exc_info=True)
        return Response({'msg': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    if not settings.BETA_FEAT_ON:
        jira_urls = []
        zendesk_urls = []
        confluence_urls = []

    if not pdf_files and not youtube_urls and not website_urls and not github_urls and not jira_urls and not zendesk_urls and not confluence_urls:
        return Response({'msg': 'No data sources provided'}, status=status.HTTP_400_BAD_REQUEST)

    service = DataSourceService(guru_type_object, request.user)
    
    try:
        # Validate limits
        service.validate_pdf_files(pdf_files, pdf_privacies)
        service.validate_url_limits(youtube_urls, 'youtube')
        service.validate_url_limits(website_urls, 'website')
        service.validate_url_limits(jira_urls, 'jira')
        service.validate_url_limits(zendesk_urls, 'zendesk')
        service.validate_url_limits(confluence_urls, 'confluence')

        if jira_urls:
            service.validate_integration('jira')
        if zendesk_urls:
            service.validate_integration('zendesk')
        if confluence_urls:
            service.validate_integration('confluence')

        # Create data sources
        results = service.create_data_sources(
            pdf_files=pdf_files,
            pdf_privacies=pdf_privacies,
            youtube_urls=youtube_urls,
            website_urls=website_urls,
            jira_urls=jira_urls,
            zendesk_urls=zendesk_urls,
            confluence_urls=confluence_urls
        )
        
        return Response({
            'msg': 'Data sources processing completed',
            'results': results
        }, status=status.HTTP_200_OK)
    except ValueError as e:
        return Response({'msg': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        logger.error(f'Error creating data sources: {e}', exc_info=True)
        return Response({'msg': 'Internal server error'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@jwt_auth
def get_data_sources_detailed(request, guru_type):
    class DataSourcePagination(PageNumberPagination):
        page_size = 10_000
        page_size_query_param = 'page_size'
        max_page_size = 10_000

    try:
        guru_type_object = get_guru_type_object_by_maintainer(guru_type, request)
    except PermissionError:
        return Response({'msg': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    except NotFoundError:
        return Response({'msg': f'Guru type {guru_type} not found'}, status=status.HTTP_404_NOT_FOUND)

    data_sources_queryset = DataSource.objects.filter(guru_type=guru_type_object).order_by('type', 'url')
    
    paginator = DataSourcePagination()
    paginated_data_sources = paginator.paginate_queryset(data_sources_queryset, request)
    serializer = DataSourceSerializer(paginated_data_sources, many=True)
    
    return paginator.get_paginated_response(serializer.data)

def delete_data_sources(request, guru_type):
    guru_type_object = get_guru_type_object(guru_type, only_active=False)

    if 'ids' not in request.data:
        return Response({'msg': 'No data sources provided'}, status=status.HTTP_400_BAD_REQUEST)

    datasource_ids = request.data.get('ids', [])
    service = DataSourceService(guru_type_object, request.user)
    
    try:
        service.delete_data_sources(datasource_ids)
        return Response({'msg': 'Data sources deleted successfully'}, status=status.HTTP_200_OK)
    except ValueError as e:
        return Response({'msg': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        logger.error(f'Error deleting data sources: {e}', exc_info=True)
        return Response({'msg': 'Internal server error'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["POST"])
@jwt_auth
def update_data_sources(request, guru_type):
    guru_type_object = get_guru_type_object_by_maintainer(guru_type, request)
    data_sources = request.data.get('data_sources', [])

    # Group data sources by private value for bulk update
    private_ids = []
    non_private_ids = []
    for ds in data_sources:
        if ds.get('private', False):
            private_ids.append(ds['id'])
        else:
            non_private_ids.append(ds['id'])

    # Perform bulk updates
    if private_ids:
        DataSource.objects.filter(
            id__in=private_ids,
            guru_type=guru_type_object,
            type=DataSource.Type.PDF
        ).update(private=True)

    if non_private_ids:
        DataSource.objects.filter(
            id__in=non_private_ids,
            guru_type=guru_type_object,
            type=DataSource.Type.PDF
        ).update(private=False)

    return Response({'msg': 'Data sources updated successfully'}, status=status.HTTP_200_OK)

@api_view(['POST'])
@auth
def data_sources(request, guru_type):
    validate_guru_type(guru_type, only_active=False)
    return create_data_sources(request, guru_type)

@api_view(['POST', 'DELETE'])
@jwt_auth
def data_sources_frontend(request, guru_type):
    try:
        guru_type_obj = get_guru_type_object_by_maintainer(guru_type, request)
    except PermissionError:
        return Response({'msg': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    except NotFoundError:
        return Response({'msg': f'Guru type {guru_type} not found'}, status=status.HTTP_404_NOT_FOUND)
    
    if request.method == 'POST':
        if settings.ENV == 'selfhosted':
            user = None
        else:
            user = get_auth0_user(request.auth0_id)
        
        # Check PDF file limits
        pdf_files = request.FILES.getlist('pdf_files', [])
        for pdf_file in pdf_files:
            is_allowed, error_msg = guru_type_obj.check_datasource_limits(user, file=pdf_file)
            if not is_allowed:
                return Response({'msg': error_msg}, status=status.HTTP_400_BAD_REQUEST)
                
        # Check website limits
        website_urls = json.loads(request.data.get('website_urls', '[]'))
        if website_urls:
            is_allowed, error_msg = guru_type_obj.check_datasource_limits(user, website_urls_count=len(website_urls))
            if not is_allowed:
                return Response({'msg': error_msg}, status=status.HTTP_400_BAD_REQUEST)
                
        # Check YouTube limits
        youtube_urls = json.loads(request.data.get('youtube_urls', '[]'))
        if youtube_urls:
            is_allowed, error_msg = guru_type_obj.check_datasource_limits(user, youtube_urls_count=len(youtube_urls))
            if not is_allowed:
                return Response({'msg': error_msg}, status=status.HTTP_400_BAD_REQUEST)

        # Check GitHub repo limits
        github_urls = json.loads(request.data.get('github_urls', '[]'))
        if github_urls:
            is_allowed, error_msg = guru_type_obj.check_datasource_limits(user, github_urls_count=len(github_urls))
            if not is_allowed:
                return Response({'msg': error_msg}, status=status.HTTP_400_BAD_REQUEST)

        # Check Jira issue limits
        jira_urls = json.loads(request.data.get('jira_urls', '[]'))
        if jira_urls:
            is_allowed, error_msg = guru_type_obj.check_datasource_limits(user, jira_urls_count=len(jira_urls))
            if not is_allowed:
                return Response({'msg': error_msg}, status=status.HTTP_400_BAD_REQUEST)
        
        # Check Zendesk ticket limits
        zendesk_urls = json.loads(request.data.get('zendesk_urls', '[]'))
        if zendesk_urls:
            is_allowed, error_msg = guru_type_obj.check_datasource_limits(user, zendesk_urls_count=len(zendesk_urls))
            if not is_allowed:
                return Response({'msg': error_msg}, status=status.HTTP_400_BAD_REQUEST)

        # Check Confluence page limits
        confluence_urls = json.loads(request.data.get('confluence_urls', '[]'))
        if confluence_urls:
            is_allowed, error_msg = guru_type_obj.check_datasource_limits(user, confluence_urls_count=len(confluence_urls))
            if not is_allowed:
                return Response({'msg': error_msg}, status=status.HTTP_400_BAD_REQUEST)                

        return create_data_sources(request, guru_type)
    elif request.method == 'DELETE':
        return delete_data_sources(request, guru_type)

@api_view(['POST'])
@jwt_auth
def data_sources_reindex(request, guru_type):
    try:
        guru_type_object = get_guru_type_object_by_maintainer(guru_type, request)
    except PermissionError:
        return Response({'msg': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    except NotFoundError:
        return Response({'msg': f'Guru type {guru_type} not found'}, status=status.HTTP_404_NOT_FOUND)
    
    datasource_ids = request.data.get('ids', [])
    
    service = DataSourceService(guru_type_object, request.user)
    
    try:
        service.reindex_data_sources(datasource_ids)
        return Response({'msg': 'Data sources reindexed successfully'}, status=status.HTTP_200_OK)
    except ValueError as e:
        return Response({'msg': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        logger.error(f'Error reindexing data sources: {e}', exc_info=True)
        return Response({'msg': 'Internal server error'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['PUT'])
@jwt_auth
def update_guru_type(request, guru_type):
    try:
        guru_type_object = get_guru_type_object_by_maintainer(guru_type, request)
    except PermissionError:
        return Response({'msg': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    except NotFoundError:
        return Response({'msg': f'Guru type {guru_type} not found'}, status=status.HTTP_404_NOT_FOUND)

    data = request.data
    domain_knowledge = data.get('domain_knowledge', guru_type_object.prompt_map['domain_knowledge'])
    intro_text = data.get('intro_text', guru_type_object.intro_text)
    github_repos = data.get('github_repos', guru_type_object.github_repos)

    try:
        github_repos = json.loads(github_repos)
    except Exception as e:
        logger.error(f'Error while parsing github repos: {e}', exc_info=True)
        return Response({'msg': 'Github repos must be a list of strings'}, status=status.HTTP_400_BAD_REQUEST)
    
    # Handle image upload if provided
    image = request.FILES.get('icon_image')
    if image:
        error, split = validate_image(image)
        if error:
            return Response({'msg': error}, status=status.HTTP_400_BAD_REQUEST)

        name_without_extension = split[0].replace(' ', '_')
        extension = split[1]

        error, icon_url = upload_image_to_storage(image, name_without_extension, extension)
        if error:
            return Response({'msg': error}, status=status.HTTP_400_BAD_REQUEST)
        guru_type_object.icon_url = icon_url

    # Update other fields
    guru_type_object.domain_knowledge = domain_knowledge
    guru_type_object.intro_text = intro_text
    guru_type_object.github_repos = github_repos
    try:
        guru_type_object.save()
    except ValidationError as e:
        return Response({'msg': str(e.message_dict['msg'][0])}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        logger.error(f'Error while updating guru type: {e}', exc_info=True)
        return Response({'msg': 'Error updating guru type'}, status=status.HTTP_400_BAD_REQUEST)
    
    return Response(GuruTypeInternalSerializer(guru_type_object).data, status=status.HTTP_200_OK)


@api_view(['DELETE'])
@jwt_auth
def delete_guru_type(request, guru_type):
    try:
        guru_type_object = get_guru_type_object_by_maintainer(guru_type, request)
    except PermissionError:
        return Response({'msg': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    except NotFoundError:
        return Response({'msg': f'Guru type {guru_type} not found'}, status=status.HTTP_404_NOT_FOUND)

    try:
        guru_type_object.delete()
    except Exception as e:
        logger.error(f'Error while deleting guru type: {e}', exc_info=True)
        return Response({'msg': 'Error deleting guru type'}, status=status.HTTP_400_BAD_REQUEST)

    return Response({'msg': 'Guru type deleted successfully'}, status=status.HTTP_200_OK)

@api_view(['GET'])
@auth
def guru_type_status(request, guru_type):
    guru_type_object: GuruType = get_guru_type_object(guru_type, only_active=False)
    is_ready = guru_type_object.ready
    return Response({'ready': is_ready}, status=status.HTTP_200_OK)


@api_view(['GET'])
@auth
def export_datasources(request):
    guru_type = request.data.get('guru_type', None)
    if not guru_type:
        return Response({"msg": "guru_type is required"}, status=status.HTTP_400_BAD_REQUEST)
    try:
        guru_type_object = get_guru_type_object(guru_type, only_active=False)
    except Exception as e:
        return Response({"msg": "Guru type does not exist"}, status=status.HTTP_400_BAD_REQUEST)
    try:
        data = {
            'data_sources': list(DataSource.objects.filter(guru_type=guru_type_object).values()),
        }
        return Response(data, status=status.HTTP_200_OK)
    except Exception as e:
        return Response({"msg": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@auth
def export_questions(request):
    guru_type = request.data.get('guru_type', None)
    if not guru_type:
        return Response({"msg": "guru_type is required"}, status=status.HTTP_400_BAD_REQUEST)
    try:
        guru_type_object = get_guru_type_object(guru_type, only_active=False)
    except Exception as e:
        return Response({"msg": "Guru type does not exist"}, status=status.HTTP_400_BAD_REQUEST)
    try:
        data = {
            'questions': list(Question.objects.filter(guru_type=guru_type_object).values()),
        }
        return Response(data, status=status.HTTP_200_OK)
    except Exception as e:
        return Response({"msg": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@follow_up_examples_auth
def follow_up_examples(request, guru_type):
    user = request.user
    
    if not settings.GENERATE_FOLLOW_UP_EXAMPLES:
        return Response([], status=status.HTTP_200_OK)
    
    guru_type_object = get_guru_type_object(guru_type, only_active=True)
    
    binge_id = request.data.get('binge_id')
    question_slug = request.data.get('question_slug')
    question_text = request.data.get('question')
    widget = request.widget if hasattr(request, 'widget') else False
    
    if not question_slug and not question_text:
        return Response({'msg': 'Question slug is required'}, status=status.HTTP_400_BAD_REQUEST)

    if binge_id:
        try:
            binge = Binge.objects.get(id=binge_id)
        except Binge.DoesNotExist:
            return Response({'msg': 'Binge does not exist'}, status=status.HTTP_400_BAD_REQUEST)
    else:
        binge = None

    if binge and not widget and not check_binge_auth(binge, user):
        return Response({'msg': 'User does not have access to this binge'}, status=status.HTTP_401_UNAUTHORIZED)

        
    last_question = search_question(
        user, 
        guru_type_object, 
        binge, 
        question_slug, 
        question_text,
        only_widget=widget,
        will_check_binge_auth=not widget
    )
    if not last_question:
        return Response({'msg': 'Question does not exist'}, status=status.HTTP_400_BAD_REQUEST)
    
    if last_question.follow_up_questions:
        return Response(last_question.follow_up_questions, status=status.HTTP_200_OK)
    
    # Get question history
    questions = [{'question': last_question.question, 'user_question': last_question.user_question}]
    ptr = last_question
    while ptr.parent:
        questions.append({'question': ptr.parent.question, 'user_question': ptr.parent.user_question})
        ptr = ptr.parent
    questions.reverse()  # Put in chronological order
    
    # Get relevant contexts from the last question
    contexts = []
    if last_question.processed_ctx_relevances and 'kept' in last_question.processed_ctx_relevances:
        for ctx in last_question.processed_ctx_relevances['kept']:
            # Skip GitHub repo contexts
            try:
                # Extract metadata using regex pattern that matches any context number
                context_parts = ctx['context'].split('\nContext ')
                metadata_text = context_parts[1].split(' Text:')[0]
                metadata_json = metadata_text.split('Metadata:\n')[1].replace("'", '"')
                metadata = json.loads(metadata_json)
                if metadata.get('type') == 'GITHUB_REPO':
                    continue
            except (json.JSONDecodeError, IndexError, KeyError):
                pass  # If we can't parse metadata, include the context
            contexts.append(ctx['context'])
    
    if not contexts:
        return Response([], status=status.HTTP_200_OK)
    
    # Generate follow-up questions using Gemini
    if settings.ENV == 'selfhosted':
        requester = OpenAIRequester()
    else:
        requester = GeminiRequester(settings.LARGE_GEMINI_MODEL)

    follow_up_examples = requester.generate_follow_up_questions(
        questions=questions,
        last_content=last_question.content,
        guru_type=guru_type_object,
        contexts=contexts
    )
    
    # Save and return the generated questions
    last_question.follow_up_questions = follow_up_examples
    last_question.save()
    
    return Response(follow_up_examples, status=status.HTTP_200_OK)


@api_view(['GET'])
@combined_auth
def follow_up_graph(request, guru_type):
    guru_type_obj = get_guru_type_object(guru_type)
    user = request.user

    binge_id = request.query_params.get('binge_id')
    if not binge_id:
        return Response({'msg': 'Binge ID is required'}, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        binge = Binge.objects.get(id=binge_id)
    except Binge.DoesNotExist:
        return Response({'msg': 'Binge does not exist'}, status=status.HTTP_400_BAD_REQUEST)
    
    if not check_binge_auth(binge, user):
        return Response({'msg': 'User does not have access to this binge'}, status=status.HTTP_401_UNAUTHORIZED)
    
    graph_nodes = Question.objects.filter(binge=binge, guru_type=guru_type_obj)
    
    # Format the response
    graph_data = []
    for node in graph_nodes:
        node_data = {
            'id': node.id,
            'slug': node.slug,
            'question': node.question,
            'parent_id': node.parent.id if node.parent else None,
            'date_created': node.date_created.isoformat(),
        }
        graph_data.append(node_data)
        
    last_usage = binge.last_used
    
    if settings.ENV == 'selfhosted':
        binge_outdated = False
    else:
        binge_outdated = last_usage < datetime.now(UTC) - timedelta(seconds=settings.FOLLOW_UP_QUESTION_TIME_LIMIT_SECONDS)

    result = {
        'question_count': len(graph_nodes),
        'graph_data': graph_data,
        'binge_outdated': binge_outdated,
    }
    
    return Response(result, status=status.HTTP_200_OK)

    
@api_view(['POST'])
@jwt_auth
def create_binge(request, guru_type):
    guru_type_object = get_guru_type_object(guru_type, only_active=True)
    if settings.ENV == 'selfhosted':
        user = None
    else:
        if request.user.is_anonymous:
            user = None
        else:
            user = request.user
    
    root_slug = request.data.get('root_slug')
    if not root_slug:
        return Response({'msg': 'Root slug is required'}, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        root_question = Question.objects.get(slug=root_slug, guru_type=guru_type_object, binge=None)
    except Question.DoesNotExist:
        return Response({'msg': 'Root question does not exist'}, status=status.HTTP_400_BAD_REQUEST)
    
    binge = create_binge_helper(guru_type_object, user, root_question)
    
    return Response({'id': str(binge.id), 'root_slug': root_slug}, status=status.HTTP_200_OK)


@api_view(['GET'])
@jwt_auth
def get_binges(request):
    user = request.user
    today = datetime.now(UTC).date()
    week_ago = today - timedelta(days=7)
    
    page_num = request.query_params.get('page_num', 1)
    search_query = request.query_params.get('search_query', '').strip()
    
    try:
        page_num = int(page_num)
    except ValueError:
        return Response({'msg': 'Page number must be an integer'}, status=status.HTTP_400_BAD_REQUEST)
    
    page_size = settings.BINGE_HISTORY_PAGE_SIZE

    binges = Binge.objects.exclude(root_question__source__in=[Question.Source.DISCORD, Question.Source.SLACK, Question.Source.GITHUB])
    
    # Base queryset
    if settings.ENV == 'selfhosted' or user.is_admin:
        binges = binges.order_by('-last_used')
    else:
        binges = binges.filter(owner=user).order_by('-last_used')
    
    # Apply search filter if search query exists
    if search_query:
        binges = binges.filter(
            Q(root_question__question__icontains=search_query) |
            Q(root_question__user_question__icontains=search_query) |
            Q(root_question__slug__icontains=search_query)
        )
    
    # Get paginated binges
    page_start = page_size * (page_num - 1)
    page_end = page_start + page_size
    
    paginated_binges = binges[page_start:page_end]
    if len(binges[page_end:]) > 0:
        has_more = True
    else:
        has_more = False
    
    # Group paginated binges by time periods
    grouped_binges = {
        'today': [],
        'last_week': [], 
        'older': []
    }

    for binge in paginated_binges:
        binge_date = binge.last_used.date()
        if binge_date == today:
            grouped_binges['today'].append(binge)
        elif week_ago <= binge_date < today:
            grouped_binges['last_week'].append(binge)
        else:
            grouped_binges['older'].append(binge)

    binges = grouped_binges

    response = {
        'today': BingeSerializer.serialize_binges(binges['today']),
        'last_week': BingeSerializer.serialize_binges(binges['last_week']),
        'older': BingeSerializer.serialize_binges(binges['older']),
        'has_more': has_more,
    }

    return Response(response, status=status.HTTP_200_OK)

@api_view(['GET', 'POST', 'DELETE'])
@jwt_auth
def api_keys(request):
    if settings.ENV == 'selfhosted':
        user = User.objects.get(email=settings.ROOT_EMAIL)
    else:
        user = request.user
    
    if request.method == 'GET':
        api_keys = APIKey.objects.filter(user=user, integration=False)
        return Response(APIKeySerializer(api_keys, many=True).data, status=status.HTTP_200_OK)
    elif request.method == 'POST':
        # Check if user has reached the limit
        existing_keys_count = APIKey.objects.filter(user=user, integration=False).count()
        if existing_keys_count >= 5:
            return Response({'msg': 'You have reached the maximum limit of 5 API keys'}, status=status.HTTP_400_BAD_REQUEST)
            
        key = "gb-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=30))
        api_key = APIKey.objects.create(user=user, name=request.data.get('name'), key=key)
        return Response({'msg': 'API key created successfully', 'key': key}, status=status.HTTP_200_OK)
    elif request.method == 'DELETE':
        try:
            api_key = APIKey.objects.get(key=request.data.get('api_key'), user=user, integration=False)
            api_key.delete()
            return Response({'msg': 'API key deleted successfully'}, status=status.HTTP_200_OK)
        except APIKey.DoesNotExist:
            return Response({'msg': 'API key does not exist'}, status=status.HTTP_400_BAD_REQUEST)

@api_view(['GET'])
def health_check(request):
    return Response({'status': 'healthy'}, status=status.HTTP_200_OK)


@api_view(['POST'])
@widget_id_auth
def ask_widget(request):
    """
    Widget endpoint for answering questions.
    Supports both streaming and non-streaming responses.
    """
    # Initialize response handler
    response_handler = WidgetResponseHandler()
    
    # Get guru type
    guru_type_object = request.guru_type
    
    # Get request parameters
    question = request.data.get('question')
    binge_id = request.data.get('binge_id')
    parent_slug = request.data.get('parent_slug')
    fetch_existing = request.data.get('fetch_existing', False)

    # Initialize with default values
    binge = None
    parent = None

    # Handle binge if provided
    if binge_id:
        try:
            binge = Binge.objects.get(id=binge_id)
        except Binge.DoesNotExist:
            return response_handler.handle_error_response("Binge not found")

        if not parent_slug:
            return response_handler.handle_error_response("Parent question slug is required")

        try:
            parent = search_question(
                None, 
                guru_type_object, 
                binge, 
                parent_slug, 
                will_check_binge_auth=False,
                only_widget=True
            )
        except Exception as e:
            return response_handler.handle_error_response("Parent question does not exist")

        if not parent:
            return response_handler.handle_error_response("Parent question does not exist")

    # Set fetch_existing to True for non-binge questions
    # if not fetch_existing and not binge:
    #     fetch_existing = True


    # Get widget response
    widget_response = api_ask(
        question=question,
        guru_type=guru_type_object,
        binge=binge,
        parent=parent,
        fetch_existing=fetch_existing,
        api_type=APIType.WIDGET,
        user=None
    )
    
    # Handle error case
    if widget_response.error:
        return response_handler.handle_error_response(widget_response.error)
    
    # Handle existing question case
    if widget_response.is_existing:
        return Response(response_handler.format_question_response(widget_response.question_obj))
    
    # Handle streaming case
    if isinstance(widget_response.content, Generator):
        return response_handler.handle_stream_response(widget_response.content)
    
    # Handle any other cases
    return response_handler.handle_non_stream_response(widget_response.content)

@api_view(['POST'])
@widget_id_auth
def widget_create_binge(request):
    guru_type_object = request.guru_type
    root_slug = request.data.get('root_slug')

    if not root_slug:
        return Response({'msg': 'Root question slug is required'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        root_question = Question.objects.get(slug=root_slug, guru_type=guru_type_object, binge=None)
    except Question.DoesNotExist:
        return Response({'msg': 'Root question does not exist'}, status=status.HTTP_400_BAD_REQUEST)

    binge = create_binge_helper(guru_type_object, None, root_question)

    return Response({'id': str(binge.id), 'root_slug': root_slug}, status=status.HTTP_200_OK)


@api_view(['POST', 'DELETE'])
@jwt_auth
def manage_widget_ids(request, guru_type):
    guru_type_object = get_guru_type_object(guru_type, only_active=False)

    if request.method == 'POST':
        domain_url = request.data.get('domain_url')
        if not domain_url:
            return Response({'msg': 'Domain URL is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            key = guru_type_object.generate_widget_id(domain_url)
            return Response({'widget_id': key}, status=status.HTTP_200_OK)
        except ValidationError as e:
            return Response({'msg': str(e.messages[0])}, status=status.HTTP_400_BAD_REQUEST)

    elif request.method == 'DELETE':
        widget_id = request.data.get('widget_id')
        if not widget_id:
            return Response({'msg': 'Widget ID is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            widget_id_obj = WidgetId.objects.get(guru_type=guru_type_object, key=widget_id)
        except WidgetId.DoesNotExist:
            return Response({'msg': 'Widget ID does not exist'}, status=status.HTTP_400_BAD_REQUEST)
        
        widget_id_obj.delete()
        return Response({'msg': 'Widget ID deleted successfully'}, status=status.HTTP_200_OK)


@api_view(['GET'])
@widget_id_auth
def get_guru_visuals(request):
    guru_type = request.guru_type
    response = {
        'colors': guru_type.colors,
        'icon_url': replace_media_root_with_base_url(guru_type.icon_url),
        'name': guru_type.name,
        'slug': guru_type.slug,
    }

    return Response(response, status=status.HTTP_200_OK)


@parser_classes([MultiPartParser, FormParser])
@api_view(['GET', 'POST', 'DELETE'])
@api_key_auth
@throttle_classes([ConcurrencyThrottleApiKey])
def api_data_sources(request, guru_type):
    """
    Unified endpoint for managing data sources.
    GET: Retrieve data sources with pagination
    POST: Create new data sources (YouTube URLs, website URLs)
    DELETE: Delete specified data sources
    """
    response_handler = DataSourceResponseHandler()
    
    try:
        guru_type_object = get_guru_type_object_by_maintainer(guru_type, request)
    except PermissionError:
        return response_handler.handle_error_response('Forbidden', status.HTTP_403_FORBIDDEN)
    except NotFoundError:
        return response_handler.handle_error_response(f'Guru type {guru_type} not found', status.HTTP_404_NOT_FOUND)

    if request.method == 'GET':
        class DataSourcePagination(PageNumberPagination):
            page_size = 1000
            page_size_query_param = 'page_size'
            max_page_size = 1000
            
        data_sources_queryset = DataSource.objects.filter(guru_type=guru_type_object).order_by('-date_created')
        paginator = DataSourcePagination()
        paginated_data_sources = paginator.paginate_queryset(data_sources_queryset, request)
        serializer = DataSourceAPISerializer(paginated_data_sources, many=True)
        return paginator.get_paginated_response(serializer.data)

    elif request.method == 'POST':
        # Simply call the existing create_data_sources function
        return create_data_sources(request, guru_type)

    elif request.method == 'DELETE':
        return delete_data_sources(request, guru_type) 

@api_view(['POST'])
@api_key_auth
@throttle_classes([ConcurrencyThrottleApiKey])
def api_answer(request, guru_type):
    """
    API endpoint for answering questions.
    Supports both streaming and non-streaming responses.
    Creates a binge for root questions and supports follow-up questions within a binge.
    """
    # Initialize response handler
    response_handler = APIResponseHandler()
    
    # Get guru type
    try:
        guru_type_object = get_guru_type_object(guru_type)
    except Exception as e:
        return response_handler.handle_error_response(e)
    
    # Get request parameters
    question = request.data.get('question')
    stream = request.data.get('stream', False)
    binge_id = request.data.get('session_id')
    fetch_existing = request.data.get('fetch_existing', False)
    user = request.user
    api_type = request.api_type  # This is now set by the api_key_auth decorator

    github_api_url = None
    if api_type == APIType.GITHUB:
        github_api_url = request.data.get('github_api_url')

    # Initialize with default values
    binge = None
    parent = None

    # Handle binge if provided
    if binge_id:
        try:
            binge = Binge.objects.get(id=binge_id)
        except Exception:
            return response_handler.handle_error_response("Session not found. Code: S-900")
        
        if not check_binge_auth(binge, user):
            return response_handler.handle_error_response("Session not found. Code: S-901")
        
        # Find the last question in the binge as the parent
        parent = Question.objects.filter(binge=binge).order_by('-date_updated').first()

    github_comments = None
    if github_api_url:
        github_handler = GithubAppHandler(request.integration)
        github_issue = github_handler.get_issue(github_api_url, request.external_id)
        github_comments = github_handler.get_issue_comments(github_api_url, request.external_id)
        github_comments.append(github_issue) # Add it as last since the helper reverses the comments before processing
        github_comments = github_handler.strip_and_format_issue_comments(github_comments, request.integration.github_bot_name)
        github_comments = github_handler.limit_issue_comments_by_length(github_comments)

    # Get API response
    api_response = api_ask(
        question=question,
        guru_type=guru_type_object,
        binge=binge,
        parent=parent,
        fetch_existing=fetch_existing,
        api_type=api_type,
        user=user,
        github_comments=github_comments
    )
    
    # Handle error case
    if api_response.error:
        return response_handler.handle_error_response(api_response.error)
    
    # Handle existing question case
    if api_response.is_existing:
        if stream:
            return response_handler.handle_stream_response(api_response.content)
        response_data = response_handler.format_question_response(api_response.question_obj)
        # Create binge if it's a root question and no binge exists
        if not binge and not parent:
            binge = create_binge_helper(guru_type_object, user, api_response.question_obj)
            response_data['session_id'] = str(binge.id)
        elif binge and not binge.root_question:
            # Used in Slack and Discord bots
            binge.root_question = api_response.question_obj
            binge.save()
        return Response(response_data)
    
    # Handle streaming case
    if isinstance(api_response.content, Generator):
        if stream:
            return response_handler.handle_stream_response(api_response.content)
        
        # Process non-streaming response for generator content
        for _ in api_response.content:
            pass

        # Fetch updated question
        if not question:
            question = api_response.question
        try:
            result = search_question(
                user=user,
                guru_type_object=guru_type_object,
                binge=binge,
                slug=None,
                question=question,
                will_check_binge_auth=False,
                include_api=True
            )
            api_response = APIAskResponse.from_existing(result)
            response_data = response_handler.format_question_response(api_response.question_obj)
            # Create binge if it's a root question and no binge exists
            if not binge and not parent:
                binge = create_binge_helper(guru_type_object, user, api_response.question_obj)
                response_data['session_id'] = str(binge.id)
            return Response(response_data)
        except Exception as e:
            return response_handler.handle_error_response(e)
    
    # Handle any other cases
    return response_handler.handle_non_stream_response(api_response.content)

# @api_view(['PUT'])
# @api_key_auth
# @throttle_classes([ConcurrencyThrottleApiKey])
# def api_update_data_source_privacy(request, guru_type):
#     """Update privacy settings for data sources."""
#     response_handler = DataSourceResponseHandler()
    
#     try:
#         guru_type_object = get_guru_type_object_by_maintainer(guru_type, request)
#     except (PermissionError, NotFoundError) as e:
#         return response_handler.handle_error_response(str(e), status.HTTP_403_FORBIDDEN)

#     try:
#         service = DataSourceService(guru_type_object, request.user)
#         data_sources = request.data
#         if len(data_sources) == 0:
#             return response_handler.handle_error_response('No data sources provided', status.HTTP_400_BAD_REQUEST)
        
#         service.update_privacy_settings(data_sources)
#         return response_handler.handle_success_response('Data sources updated successfully')
#     except ValueError as e:
#         return response_handler.handle_error_response(str(e))
#     except Exception as e:
#         return response_handler.handle_error_response(f'Unexpected error: {str(e)}')


@api_view(['POST'])
@api_key_auth
@throttle_classes([ConcurrencyThrottleApiKey])
def api_reindex_data_sources(request, guru_type):
    """Reindex specified data sources."""
    response_handler = DataSourceResponseHandler()
    
    try:
        guru_type_object = get_guru_type_object_by_maintainer(guru_type, request)
    except (PermissionError, NotFoundError) as e:
        return response_handler.handle_error_response(str(e), status.HTTP_403_FORBIDDEN)

    try:
        service = DataSourceService(guru_type_object, request.user)
        datasource_ids = request.data.get('ids', [])
        if len(datasource_ids) == 0:
            return response_handler.handle_error_response('No data sources provided', status.HTTP_400_BAD_REQUEST)
        
        service.reindex_data_sources(datasource_ids)
        return response_handler.handle_success_response('Data sources reindexed successfully')
    except ValueError as e:
        return response_handler.handle_error_response(str(e))
    except Exception as e:
        return response_handler.handle_error_response(f'Unexpected error: {str(e)}')


@api_view(['GET'])
def create_integration(request):
    # OAuth flow
    code = request.query_params.get('code')
    state = request.query_params.get('state')

    if not state:
        return Response({
            'msg': 'Missing required parameters'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Decode the state parameter
        state_json = json.loads(state)
        integration_type = state_json.get('type')
        guru_type_slug = state_json.get('guru_type')
        encoded_guru_slug = state_json.get('encoded_guru_slug')
        installation_id = state_json.get('installation_id')

        if not all([integration_type, guru_type_slug, encoded_guru_slug]):
            return Response({
                'msg': 'Invalid state parameter'
            }, status=status.HTTP_400_BAD_REQUEST)

        decoded_guru_slug = decode_guru_slug(encoded_guru_slug)
        if not decoded_guru_slug or decoded_guru_slug != guru_type_slug:
            return Response({
                'msg': 'Invalid state parameter'
            }, status=status.HTTP_400_BAD_REQUEST)                    

    except Exception as e:
        logger.error(f"Error creating integration: {e}", exc_info=True)
        return Response({
            'msg': 'There has been an error while creating the integration. Please try again. If the problem persists, please contact support.'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        guru_type = GuruType.objects.get(slug=guru_type_slug)
    except GuruType.DoesNotExist:
        return Response({
            'msg': 'Invalid guru type'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        strategy = IntegrationFactory.get_strategy(integration_type)
        if integration_type.upper() == 'GITHUB':
            if not installation_id:
                return Response({
                    'msg': 'Missing installation_id for GitHub integration'
                }, status=status.HTTP_400_BAD_REQUEST)
            integration = strategy.create_integration(installation_id, guru_type)
        else:
            if not code:
                return Response({
                    'msg': 'Missing code parameter'
                }, status=status.HTTP_400_BAD_REQUEST)
            integration = strategy.create_integration(code, guru_type)
    except IntegrationError as e:
        return Response({
            'msg': str(e)
        }, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        logger.error(f"Error creating integration: {e}", exc_info=True)
        return Response({
            'msg': 'There has been an error while creating the integration. Please try again. If the problem persists, please contact support.'
        }, status=status.HTTP_400_BAD_REQUEST)

    return Response({
        'id': integration.id,
        'type': integration.type,
        'guru_type': guru_type.slug,
        'channels': integration.channels
    })



@api_view(['GET', 'DELETE', 'POST'])
@jwt_auth
def manage_integration(request, guru_type, integration_type):
    """
    GET: Get integration details for a specific guru type and integration type.
    DELETE: Delete an integration and invalidate its OAuth token.
    POST: Create a new integration.
    """
    try:
        guru_type_object = get_guru_type_object_by_maintainer(guru_type, request)
    except PermissionError:
        return Response({'msg': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    except NotFoundError:
        return Response({'msg': f'Guru type {guru_type} not found'}, status=status.HTTP_404_NOT_FOUND)
        
    # Validate integration type
    if integration_type not in [choice.value for choice in Integration.Type]:
        return Response({'msg': f'Invalid integration type: {integration_type}'}, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        if request.method in ['GET', 'DELETE']:
            integration = Integration.objects.get(
                guru_type=guru_type_object,
                type=integration_type
            )
        else:
            integration = None

        if request.method == 'GET':
            command = GetIntegrationCommand(integration)
            return command.execute()
        elif request.method == 'DELETE':
            command = DeleteIntegrationCommand(integration, guru_type_object)
            return command.execute()
        elif request.method == 'POST':
            command = CreateIntegrationCommand(guru_type_object, integration_type, request.data)
            return command.execute()
            
    except Integration.DoesNotExist:
        if request.method == 'GET':
            return Response({"encoded_guru_slug": encode_guru_slug(guru_type_object.slug)}, status=status.HTTP_202_ACCEPTED)
        else:
            return Response({'msg': 'Integration not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Error in manage_integration: {e}", exc_info=True)
        return Response({'msg': 'Internal server error'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['GET', 'POST'])
@jwt_auth
def manage_channels(request, guru_type, integration_type):
    """Get or update channels for a specific integration type of a guru type."""
    try:
        guru_type_object = get_guru_type_object_by_maintainer(guru_type, request)
    except PermissionError:
        return Response({'msg': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    except NotFoundError:
        return Response({'msg': f'Guru type {guru_type} not found'}, status=status.HTTP_404_NOT_FOUND)
        
    # Validate integration type
    if integration_type not in [choice.value for choice in Integration.Type]:
        return Response({'msg': f'Invalid integration type: {integration_type}'}, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        integration = Integration.objects.get(
            guru_type=guru_type_object,
            type=integration_type
        )
    except Integration.DoesNotExist:
        return Response(status=status.HTTP_204_NO_CONTENT)

    if request.method == 'POST':
        try:
            channels = request.data.get('channels', [])
            integration.channels = channels
            integration.save()
            
            return Response({
                'id': integration.id,
                'type': integration.type,
                'guru_type': integration.guru_type.slug,
                'channels': integration.channels
            })
        except Exception as e:
            logger.error(f"Error updating channels: {e}", exc_info=True)
            return Response({'msg': 'Internal server error'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    try:
        # Get channels from API
        strategy = IntegrationFactory.get_strategy(integration_type, integration)
        api_channels = strategy.list_channels()

        processed_channels = []
        if integration_type == 'GITHUB':
            # Create a map of channel IDs to their allowed status from DB
            db_channels_map = {
                channel['id']: channel['mode']
                for channel in integration.channels
            }
            # Process each API channel
            for channel in api_channels:
                channel['mode'] = db_channels_map.get(channel['id'], 'auto')
                processed_channels.append(channel)

            # Save the new channels, we save them in retrieval because it is set on GitHub settings. We only read what is set. We can't update them.
            integration.channels = processed_channels
            integration.save()
        else:
            # Create a map of channel IDs to their allowed status from DB
            db_channels_map = {
                channel['id']: channel['allowed']
                for channel in integration.channels
            }
            # Process each API channel
            for channel in api_channels:
                # If channel exists in DB, use its allowed status
                # Otherwise, default to False for new channels
                channel['allowed'] = db_channels_map.get(channel['id'], False)
                processed_channels.append(channel)
        
        return Response({
            'channels': processed_channels
        })
    except Exception as e:
        logger.error(f"Error listing channels: {e}", exc_info=True)
        keyword = 'repositories' if integration_type == 'GITHUB' else 'channels'
        return Response({'msg': f'Error listing {keyword}. Please make sure the integration is valid.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

def get_or_create_thread_binge(thread_id: str, integration: Integration) -> tuple[Thread, Binge]:
    """Get or create a thread and its associated binge."""
    try:
        thread = Thread.objects.get(thread_id=thread_id, integration=integration)
        return thread, thread.binge
    except Thread.DoesNotExist:
        # Create new binge without a root question
        binge = create_fresh_binge(integration.guru_type, None)
        thread = Thread.objects.create(
            thread_id=thread_id,
            binge=binge,
            integration=integration
        )
        return thread, binge

def strip_first_header(content: str) -> str:
    """Remove the first header (starting with # and ending with newline) from content."""
    if content.startswith('#'):
        # Find the first newline
        newline_index = content.find('\n')
        if newline_index != -1:
            # Return content after the newline
            return content[newline_index + 1:].lstrip()
    return content

def convert_markdown_to_slack(content: str) -> str:
    """Convert Markdown formatting to Slack formatting."""
    # Convert markdown code blocks to Slack code blocks by removing language specifiers
    import re
    
    # First remove language specifiers from code blocks
    content = re.sub(r'```\w+', '```', content)
    
    # Then remove empty lines at the start and end of code blocks
    def trim_code_block(match):
        code_block = match.group(0)
        lines = code_block.split('\n')
        
        # Find first and last non-empty lines (excluding ```)
        start = 0
        end = len(lines) - 1
        
        # Find first non-empty line after opening ```
        for i, line in enumerate(lines):
            if line.strip() == '```':
                start = i + 1
                break
                
        # Find last non-empty line before closing ```
        for i in range(len(lines) - 1, -1, -1):
            if line.strip() == '```':
                end = i
                break
                
        # Keep all lines between start and end (inclusive)
        return '```\n' + '\n'.join(lines[start:end]) + '\n```'
    
    content = re.sub(r'```[\s\S]+?```', trim_code_block, content)
    
    # Convert markdown links [text](url) to Slack format <url|text>
    def replace_link(match):
        text = match.group(1)
        url = match.group(2)
        return f"<{url}|{text}>"
    
    content = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', replace_link, content)
    
    # Convert markdown bold/italic to Slack format
    # First handle single asterisks for italics (but not if they're part of double asterisks)
    i = 0
    while i < len(content):
        if content[i:i+2] == "**":
            i += 2
        elif content[i] == "*":
            # Replace single asterisk with underscore for italics
            content = content[:i] + "_" + content[i+1:]
        i += 1
    
    # Then handle double asterisks for bold
    content = content.replace("**", "*")
    
    return content

def format_slack_response(content: str, trust_score: int, references: list, question_url: str) -> str:
    """Format the response with trust score and references for Slack.
    Using Slack's formatting syntax:
    *bold*
    _italic_
    ~strikethrough~
    `code`
    ```preformatted```
    >blockquote
    <url|text> for links
    """
    # Strip header from content
    content = strip_first_header(content)
    
    # Convert markdown to slack formatting
    content = convert_markdown_to_slack(content)
    
    formatted_msg = [content]
    
    # Add trust score with emoji
    trust_emoji = get_trust_score_emoji(trust_score)
    formatted_msg.append(f"\n---------\n_*Trust Score*: {trust_emoji} {trust_score}_%")
    
    # Add references if they exist
    if references:
        formatted_msg.append("\n_*Sources*_:")
        for ref in references:
            # First remove Slack-style emoji codes with adjacent spaces
            clean_title = cleanup_title(ref['title'])
            
            formatted_msg.append(f"\n• _<{ref['link']}|{clean_title}>_")
    
    # Add frontend link if it exists
    if question_url:
        formatted_msg.append(f"\n:eyes: _<{question_url}|View on Gurubase for a better UX>_")
    
    return "\n".join(formatted_msg)

async def stream_and_update_message(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict,
    payload: dict,
    client: WebClient,
    channel_id: str,
    message_ts: str,
    update_interval: float = 0.5
) -> None:
    """Stream the response and update the Slack message periodically."""
    last_update = time.time()
    current_content = ""
    
    try:
        # Create request using APIRequestFactory
        factory = APIRequestFactory()
        guru_type = payload.get('guru_type')
        
        request = factory.post(
            f'/api/v1/{guru_type}/answer/',
            payload,
            HTTP_X_API_KEY=headers.get('X-API-KEY'),
            format='json'
        )
        
        # Call api_answer directly in a sync context
        response = await sync_to_async(api_answer)(request, guru_type)
        
        # Handle StreamingHttpResponse
        if hasattr(response, 'streaming_content'):
            buffer = ""
            line_buffer = ""
            
            # Create an async wrapper for the generator iteration
            @sync_to_async
            def get_next_chunk():
                try:
                    return next(response.streaming_content)
                except StopIteration:
                    return None
            
            # Iterate over the generator asynchronously
            while True:
                chunk = await get_next_chunk()
                if chunk is None:
                    # Yield any remaining text in the buffer
                    if line_buffer.strip():
                        buffer += line_buffer
                        # Strip header and convert markdown
                        cleaned_content = strip_first_header(buffer)
                        if cleaned_content.strip():
                            formatted_content = convert_markdown_to_slack(cleaned_content)
                            formatted_content += '\n\n:clock1: _streaming..._'
                            try:
                                client.chat_update(
                                    channel=channel_id,
                                    ts=message_ts,
                                    text=formatted_content
                                )
                            except SlackApiError as e:
                                logger.error(f"Error updating message: {e.response}", exc_info=True)
                    break
                    
                if chunk:
                    text = chunk.decode('utf-8') if isinstance(chunk, bytes) else str(chunk)
                    line_buffer += text
                    
                    # Check if we have complete lines
                    while '\n' in line_buffer:
                        line, line_buffer = line_buffer.split('\n', 1)
                        if line.strip():
                            buffer += line + '\n'
                            # Strip header and convert markdown
                            cleaned_content = strip_first_header(buffer)
                            if cleaned_content.strip():
                                formatted_content = convert_markdown_to_slack(cleaned_content)
                                formatted_content += '\n\n:clock1: _streaming..._'
                                current_time = time.time()
                                if current_time - last_update >= update_interval:
                                    try:
                                        client.chat_update(
                                            channel=channel_id,
                                            ts=message_ts,
                                            text=formatted_content
                                        )
                                        last_update = current_time
                                    except SlackApiError as e:
                                        logger.error(f"Error updating message: {e.response}", exc_info=True)
                                        client.chat_update(
                                            channel=channel_id,
                                            ts=message_ts,
                                            text="❌ Failed to update message"
                                        )
                                        return
    except Exception as e:
        logger.error(f"Error in stream_and_update_message: {str(e)}", exc_info=True)
        client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text="❌ An error occurred while processing your request"
        )
        return

async def get_final_response(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict,
    payload: dict,
    client: WebClient,
    channel_id: str,
    message_ts: str
) -> None:
    """Get and send the final formatted response."""
    try:
        # Create request using APIRequestFactory
        factory = APIRequestFactory()
        guru_type = payload.get('guru_type')
        
        request = factory.post(
            f'/api/v1/{guru_type}/answer/',
            payload,
            HTTP_X_API_KEY=headers.get('X-API-KEY'),
            format='json'
        )
        
        # Call api_answer directly
        response = await sync_to_async(api_answer)(request, guru_type)
        
        # Convert response to dict if it's a Response object
        if hasattr(response, 'data'):
            final_response = response.data
        else:
            final_response = response

        if 'msg' in final_response and 'doesn\'t have enough data' in final_response['msg']:
            raise NotEnoughData(final_response['msg'])
        elif 'msg' in final_response and 'is not related to' in final_response['msg']:
            raise NotRelated(final_response['msg'])
        elif 'msg' in final_response:
            raise Exception(final_response['msg'])

        trust_score = final_response.get('trust_score', 0)
        references = final_response.get('references', [])
        content = final_response.get('content', '')
        question_url = final_response.get('question_url', '')

        final_text = format_slack_response(content, trust_score, references, question_url)
        if final_text.strip():  # Only update if there's content after stripping header
            client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=final_text
            )
    except NotEnoughData as e:
        logger.error(f"Not enough data: {str(e)}", exc_info=True)
        client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=f"❌ {str(e)}"
        )
    except NotRelated as e:
        logger.error(f"Not related to the question: {str(e)}", exc_info=True)
        client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=f"❌ {str(e)}"
        )
    except Exception as e:
        logger.error(f"Error in get_final_response: {str(e)}", exc_info=True)
        client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text="❌ An error occurred while processing your request"
        )

async def handle_slack_message(
    client: WebClient,
    integration: Integration,
    channel_id: str,
    thread_ts: str,
    clean_message: str
) -> None:
    """Handle a single Slack message."""

    try:
        # First send a thinking message
        thinking_response = client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text="Thinking... 🤔"
        )
        
        try:
            # Get or create thread and binge
            thread, binge = await sync_to_async(get_or_create_thread_binge)(thread_ts, integration)
        except Exception as e:
            logger.error(f"Error creating thread/binge: {str(e)}", exc_info=True)
            client.chat_update(
                channel=channel_id,
                ts=thinking_response["ts"],
                text="❌ Failed to create conversation thread"
            )
            return
        
        guru_type_slug = await sync_to_async(lambda integration: integration.guru_type.slug)(integration)
        api_key = await sync_to_async(lambda integration: integration.api_key.key)(integration)
        
        try:
            # First get streaming response
            stream_payload = {
                'question': clean_message,
                'stream': True,
                'short_answer': True,
                'session_id': str(binge.id),
                'guru_type': guru_type_slug
            }
            
            headers = {
                'X-API-KEY': api_key,
                'Content-Type': 'application/json'
            }
            
            async with aiohttp.ClientSession() as session:
                await stream_and_update_message(
                    session=session,
                    url='',  # Not used anymore
                    headers=headers,
                    payload=stream_payload,
                    client=client,
                    channel_id=channel_id,
                    message_ts=thinking_response["ts"]
                )
                
                # Then get final formatted response
                final_payload = {
                    'question': clean_message,
                    'stream': False,
                    'short_answer': True,
                    'fetch_existing': True,
                    'session_id': str(binge.id),
                    'guru_type': guru_type_slug
                }
                
                await get_final_response(
                    session=session,
                    url='',  # Not used anymore
                    headers=headers,
                    payload=final_payload,
                    client=client,
                    channel_id=channel_id,
                    message_ts=thinking_response["ts"]
                )
        except aiohttp.ClientError as e:
            logger.error(f"Network error: {str(e)}", exc_info=True)
            client.chat_update(
                channel=channel_id,
                ts=thinking_response["ts"],
                text="❌ Network error occurred while processing your request"
            )
        except Exception as e:
            logger.error(f"Error in API communication: {str(e)}", exc_info=True)
            client.chat_update(
                channel=channel_id,
                ts=thinking_response["ts"],
                text="❌ An error occurred while processing your request"
            )
    except SlackApiError as e:
        logger.error(f"Slack API error: {str(e)}", exc_info=True)
        # If we can't even send the thinking message, we can't update it later
        try:
            if thinking_response:
                client.chat_update(
                    channel=channel_id,
                    ts=thinking_response["ts"],
                    text="❌ Failed to process your request due to a Slack API error"
                )
            else:
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text="❌ Failed to process your request due to a Slack API error"
                )
        except:
            pass  # If this fails too, we can't do much
    except Exception as e:
        logger.error(f"Unexpected error in handle_slack_message: {str(e)}", exc_info=True)
        try:
            if thinking_response:
                client.chat_update(
                    channel=channel_id,
                    ts=thinking_response["ts"],
                    text="❌ An unexpected error occurred"
                )
            else:
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text="❌ An unexpected error occurred"
                )
        except:
            pass  # If this fails too, we can't do much

async def send_channel_unauthorized_message(
    client: WebClient,
    channel_id: str,
    thread_ts: str,
    guru_slug: str
) -> None:
    """Send a message explaining how to authorize the channel."""
    try:
        settings_url = f"{settings.BASE_URL.rstrip('/')}/guru/{guru_slug}/integrations/slack"
        message = (
            "❌ This channel is not authorized to use the bot.\n\n"
            f"Please visit <{settings_url}|Gurubase Settings> to configure "
            "the bot and add this channel to the allowed channels list."
        )
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=message
        )
    except SlackApiError as e:
        logger.error(f"Error sending unauthorized channel message: {e.response}", exc_info=True)


@api_view(['GET', 'POST'])
def slack_events(request):
    """Handle Slack events including verification and message processing."""
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    import asyncio
    import threading
    from django.core.cache import caches
    
    data = request.data
    
    # If this is a verification request, respond with the challenge parameter
    if "challenge" in data:
        return Response(data["challenge"], status=status.HTTP_200_OK)
    
    # Handle the event in a separate thread
    if "event" in data:
        def process_event():
            try:
                event = data["event"]
                
                # Only proceed if it's a message event and not from a bot
                if event["type"] == "message" and "subtype" not in event and event.get("user") != event.get("bot_id"):
                    # Get bot user ID from authorizations
                    bot_user_id = data.get("authorizations", [{}])[0].get("user_id")
                    user_message = event["text"]
                    
                    # First check if the bot is mentioned
                    if not (bot_user_id and f"<@{bot_user_id}>" in user_message):
                        return
                        
                    team_id = data.get('team_id')
                    if not team_id:
                        return
                        
                    # Try to get integration from cache first
                    cache = caches['alternate']
                    cache_key = f"slack_integration:{team_id}"
                    integration = cache.get(cache_key)
                    
                    if not integration:
                        try:
                            # If not in cache, get from database
                            integration = Integration.objects.get(type=Integration.Type.SLACK, external_id=team_id)
                            # Set cache timeout to 0. This is because dynamic channel updates are not immediately reflected
                            # And this may result in bad UX, and false positive bug reports
                            cache.set(cache_key, integration, timeout=0)
                        except Integration.DoesNotExist:
                            logger.error(f"No integration found for team {team_id}", exc_info=True)
                            return
                    
                    try:
                        # Get the Slack client for this team
                        client = WebClient(token=integration.access_token)
                        
                        channel_id = event["channel"]
                        
                        # Check if the current channel is allowed
                        channels = integration.channels
                        channel_allowed = False
                        for channel in channels:
                            if str(channel.get('id')) == channel_id and channel.get('allowed', False):
                                channel_allowed = True
                                break

                        # Get thread_ts if it exists (means we're in a thread)
                        thread_ts = event.get("thread_ts") or event.get("ts")
                        
                        if not channel_allowed:
                            # Run the unauthorized message handler in the event loop
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                            try:
                                loop.run_until_complete(send_channel_unauthorized_message(
                                    client=client,
                                    channel_id=channel_id,
                                    thread_ts=thread_ts,
                                    guru_slug=integration.guru_type.slug
                                ))
                            finally:
                                loop.close()
                            return
                        
                        # Remove the bot mention from the message
                        clean_message = user_message.replace(f"<@{bot_user_id}>", "").strip()
                        
                        # Run the async handler in a new event loop
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        try:
                            loop.run_until_complete(handle_slack_message(
                                client=client,
                                integration=integration,
                                channel_id=channel_id,
                                thread_ts=thread_ts,
                                clean_message=clean_message
                            ))
                        except SlackApiError as e:
                            if e.response.data.get('msg') in ['token_expired', 'invalid_auth', 'not_authed']:
                                try:
                                    # Get fresh integration data from DB
                                    integration = Integration.objects.get(id=integration.id)
                                    # Try to refresh the token
                                    strategy = IntegrationFactory.get_strategy(integration.type, integration)
                                    new_token = strategy.handle_token_refresh()
                                    
                                    # Update cache with new integration data
                                    cache.set(cache_key, integration, timeout=300)
                                    
                                    # Retry with new token
                                    client = WebClient(token=new_token)
                                    loop.run_until_complete(handle_slack_message(
                                        client=client,
                                        integration=integration,
                                        channel_id=channel_id,
                                        thread_ts=thread_ts,
                                        clean_message=clean_message
                                    ))
                                except Exception as refresh_error:
                                    logger.error(f"Error refreshing token: {refresh_error}", exc_info=True)
                            else:
                                logger.error(f"Slack API error: {e}", exc_info=True)
                        finally:
                            loop.close()
                            
                    except Exception as e:
                        logger.error(f"Error processing Slack event: {e}", exc_info=True)
            except Exception as e:
                logger.error(f"Error in process_event thread: {e}", exc_info=True)
        
        # Start processing in a separate thread
        thread = threading.Thread(target=process_event)
        thread.daemon = True  # Make thread daemon so it doesn't block server shutdown
        thread.start()
    
    # Return 200 immediately
    return Response(status=200)

@api_view(['POST'])
@jwt_auth
def send_test_message(request):
    """Send a test message to a specific channel using the specified integration."""
    integration_id = request.data.get('integration_id')
    channel_id = request.data.get('channel_id')

    if not integration_id or not channel_id:
        return Response({'msg': 'Integration ID and channel ID are required'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        integration = Integration.objects.get(id=integration_id)
    except Integration.DoesNotExist:
        return Response({'msg': 'Integration not found'}, status=status.HTTP_404_NOT_FOUND)

    try:
        # Get the appropriate strategy for the integration type
        strategy = IntegrationFactory.get_strategy(integration.type, integration)
        success = strategy.send_test_message(channel_id)
        
        if success:
            return Response({'msg': 'Test message sent successfully'}, status=status.HTTP_200_OK)
        else:
            return Response({'msg': 'Failed to send test message'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except Exception as e:
        logger.error(f"Error sending test message: {e}", exc_info=True)
        return Response({'msg': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['GET'])
@jwt_auth
def list_integrations(request, guru_type):
    """
    GET: List all integrations for a specific guru type.
    """
    try:
        guru_type_object = get_guru_type_object_by_maintainer(guru_type, request)
    except PermissionError:
        return Response({'msg': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    except NotFoundError:
        return Response({'msg': f'Guru type {guru_type} not found'}, status=status.HTTP_404_NOT_FOUND)
    
    try:
        integrations = Integration.objects.filter(guru_type=guru_type_object)
        
        response_data = []
        for integration in integrations:
            response_data.append({
                'id': integration.id,
                'type': integration.type,
                'workspace_name': integration.workspace_name,
                'external_id': integration.external_id,
                'channels': integration.channels,
                'date_created': integration.date_created,
                'date_updated': integration.date_updated,
            })
        
        return Response(response_data, status=status.HTTP_200_OK)
            
    except Exception as e:
        logger.error(f"Error in list_integrations: {e}", exc_info=True)
        return Response({'msg': 'Internal server error'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['GET', 'PUT'])
@jwt_auth
def manage_settings(request):
    """
    GET: Retrieve current settings (excluding sensitive data like API keys)
    PUT: Update settings
    """
    from core.utils import get_default_settings
    settings_obj = get_default_settings()

    if request.method == 'GET':
        settings_obj.save() # Save to trigger validation
        settings_obj.refresh_from_db()
        serializer = SettingsSerializer(settings_obj)
        return Response(serializer.data)

    elif request.method == 'PUT':
        serializer = SettingsSerializer(settings_obj, data=request.data, partial=True)
        if serializer.is_valid():
            if not request.data.get('openai_api_key_written'):
                serializer.validated_data['openai_api_key'] = settings_obj.openai_api_key
            if not request.data.get('firecrawl_api_key_written'):
                serializer.validated_data['firecrawl_api_key'] = settings_obj.firecrawl_api_key
            if not request.data.get('youtube_api_key_written'):
                serializer.validated_data['youtube_api_key'] = settings_obj.youtube_api_key
            
            try:
                serializer.save()
                return Response(serializer.data)
            except ValidationError as e:
                logger.error(f"Validation error in manage_settings: {e}", exc_info=True)
                return Response(
                    {'errors': e.message_dict if hasattr(e, 'message_dict') else {'error': str(e)}},
                    status=status.HTTP_400_BAD_REQUEST
                )
            except IntegrityError as e:
                logger.error(f"Integrity error in manage_settings: {e}", exc_info=True)
                return Response({'msg': str(e)}, status=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                logger.error(f"Error in manage_settings: {e}", exc_info=True)
                return Response({'msg': 'Internal server error'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

@api_view(['POST'])
@auth
def parse_sitemap(request):
    """
    Parse URLs from a sitemap XML file.
    If given a sitemap index, it will fetch and parse all referenced sitemaps.
    Expects a POST request with a 'sitemap_url' parameter that ends with .xml
    Returns a list of all URLs found across all sitemaps.
    """
    sitemap_url = request.data.get('sitemap_url')
    
    if not sitemap_url:
        return Response({'msg': 'Sitemap URL is required'}, status=status.HTTP_400_BAD_REQUEST)
        
    if not sitemap_url.endswith('.xml'):
        return Response({'msg': 'Sitemap URL must end with .xml'}, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        # Validate URL format
        from urllib.parse import urlparse
        parsed_url = urlparse(sitemap_url)
        if not all([parsed_url.scheme, parsed_url.netloc]):
            return Response({'msg': 'Invalid URL format'}, status=status.HTTP_400_BAD_REQUEST)
            
        def fetch_and_parse_sitemap(url):
            """Helper function to fetch and parse a single sitemap."""
            try:
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                return ET.fromstring(response.content)
            except Exception as e:
                logger.error(f"Error fetching sitemap {url}: {e}", exc_info=True)
                return None

        def extract_urls_from_sitemap(root):
            """Extract URLs from a standard sitemap."""
            if not root:
                return []
                
            namespaces = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
            urls = []
            
            # Find all URL elements
            loc_elements = root.findall('.//ns:url/ns:loc', namespaces)
            for loc in loc_elements:
                url = loc.text.strip()
                urls.append(url)
                
            return urls

        def process_sitemap_index(root):
            """Process a sitemap index and return all URLs from referenced sitemaps."""
            if not root:
                return []
                
            namespaces = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
            all_urls = []
            
            # Find all sitemap references
            sitemap_elements = root.findall('.//ns:sitemap/ns:loc', namespaces)
            
            for sitemap in sitemap_elements:
                sitemap_url = sitemap.text.strip()
                # Fetch and parse the referenced sitemap
                sitemap_root = fetch_and_parse_sitemap(sitemap_url)
                if sitemap_root is not None:
                    # Extract URLs from this sitemap
                    urls = extract_urls_from_sitemap(sitemap_root)
                    all_urls.extend(urls)
            
            return all_urls

        # Fetch and parse the initial sitemap/index
        import requests
        from xml.etree import ElementTree as ET
        
        root = fetch_and_parse_sitemap(sitemap_url)
        if not root:
            return Response({'msg': 'Error fetching sitemap'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        # Determine if this is a sitemap index or regular sitemap
        urls = []
        if root.tag.endswith('sitemapindex'):
            # Process sitemap index
            urls = process_sitemap_index(root)
        elif root.tag.endswith('urlset'):
            # Process regular sitemap
            urls = extract_urls_from_sitemap(root)
        else:
            return Response({
                'msg': 'Invalid sitemap format. Root element must be <urlset> or <sitemapindex>.'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        if not urls:
            return Response({
                'msg': 'No URLs found in the sitemap(s)',
                'urls': [],
                'total_urls': 0
            }, status=status.HTTP_200_OK)
        
        return Response({
            'urls': urls,
            'total_urls': len(urls)
        }, status=status.HTTP_200_OK)
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching sitemap: {e}", exc_info=True)
        return Response({'msg': 'Error fetching sitemap'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except ET.ParseError as e:
        logger.error(f"Error parsing XML: {e}", exc_info=True)
        return Response({'msg': 'Invalid XML format'}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        logger.error(f"Unexpected error parsing sitemap: {e}", exc_info=True)
        return Response({'msg': 'Error processing sitemap'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['POST'])
@jwt_auth
def start_crawl_admin(request, guru_slug):
    try:
        data, return_status = CrawlService.start_crawl(
            guru_slug,
            request.user,
            request.data.get('url'),
            source=CrawlState.Source.UI
        )
    except Exception as e:
        return Response({'msg': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(data, status=return_status)

@api_view(['POST'])
@api_key_auth
@throttle_classes([ConcurrencyThrottleApiKey])
def start_crawl_api(request, guru_slug):
    try:
        data, return_status = CrawlService.start_crawl(
            guru_slug,
            request.user,
            request.data.get('url'),
            source=CrawlState.Source.API
        )
    except Exception as e:
        return Response({'msg': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(data, status=return_status)

@api_view(['POST'])
@jwt_auth
def stop_crawl_admin(request, crawl_id):
    try:
        data, return_status = CrawlService.stop_crawl(
            request.user,
            crawl_id
        )
    except Exception as e:
        return Response({'msg': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(data, status=return_status)

@api_view(['POST'])
@api_key_auth
@throttle_classes([ConcurrencyThrottleApiKey])
def stop_crawl_api(request, crawl_id):
    try:
        data, return_status = CrawlService.stop_crawl(
            request.user,
            crawl_id
        )
    except Exception as e:
        return Response({'msg': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(data, status=return_status)

@api_view(['GET'])
@jwt_auth
def get_crawl_status_admin(request, crawl_id):
    try:
        data, return_status = CrawlService.get_crawl_status(
            request.user,
            crawl_id
        )
    except Exception as e:
        return Response({'msg': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(data, status=return_status)

@api_view(['GET'])
@api_key_auth
@throttle_classes([ConcurrencyThrottleApiKey])
def get_crawl_status_api(request, crawl_id):
    try:
        data, return_status = CrawlService.get_crawl_status(
            request.user,
            crawl_id
        )
    except Exception as e:
        return Response({'msg': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(data, status=return_status)

@api_view(['POST'])
@combined_auth
def submit_guru_creation_form(request):
    """
    Handle submission of guru creation forms.
    """
    try:
        name = request.data.get('name')
        email = request.data.get('email')
        github_repo = request.data.get('github_repo')
        docs_url = request.data.get('docs_url')
        use_case = request.data.get('use_case')
        source = request.data.get('source', 'unknown')

        if not all([name, email, docs_url]):
            return Response({
                'error': 'Missing required fields. Please provide name, email, and documentation root url.'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Create form submission
        GuruCreationForm.objects.create(
            name=name,
            email=email,
            github_repo=github_repo,
            docs_url=docs_url,
            use_case=use_case,
            source=source
        )

        return Response({
            'message': 'Your guru creation request has been submitted successfully.'
        }, status=status.HTTP_201_CREATED)

    except Exception as e:
        logger.error(f'Error processing guru creation form: {e}', exc_info=True)
        return Response({
            'error': 'An error occurred while processing your request.'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['POST'])
@jwt_auth
def fetch_youtube_playlist_admin(request):
    url = request.data.get('url')
    if not url:
        return Response({'error': 'URL is required'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        data, return_status = YouTubeService.fetch_playlist(url)
    except Exception as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(data, status=return_status)

@api_view(['POST'])
@api_key_auth
@throttle_classes([ConcurrencyThrottleApiKey])
def fetch_youtube_playlist_api(request):
    url = request.data.get('url')
    if not url:
        return Response({'error': 'URL is required'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        data, return_status = YouTubeService.fetch_playlist(url)
    except Exception as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(data, status=return_status)

@api_view(['POST'])
@jwt_auth
def fetch_youtube_channel_admin(request):
    url = request.data.get('url')
    if not url:
        return Response({'error': 'URL is required'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        data, return_status = YouTubeService.fetch_channel(url)
    except Exception as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(data, status=return_status)

@api_view(['POST'])
@api_key_auth
@throttle_classes([ConcurrencyThrottleApiKey])
def fetch_youtube_channel_api(request):
    url = request.data.get('url')
    if not url:
        return Response({'error': 'URL is required'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        data, return_status = YouTubeService.fetch_channel(url)
    except Exception as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(data, status=return_status)

@api_view(['GET', 'POST'])
def github_webhook(request):
    if request.method != 'POST':
        return Response({'message': 'Webhook received'}, status=status.HTTP_200_OK)

    body = request.body
    data = request.data
    installation_id = data.get('installation', {}).get('id')
    
    if not installation_id:
        logger.error("No installation ID found in webhook payload")
        return Response({'message': 'No installation ID found'}, status=status.HTTP_400_BAD_REQUEST)

    # Try to get integration from cache first
    cache = caches['alternate']
    cache_key = f"github_integration:{installation_id}"
    integration = cache.get(cache_key)
    
    if not integration:
        try:
            # If not in cache, get from database
            integration = Integration.objects.get(type=Integration.Type.GITHUB, external_id=installation_id)
            # Set cache timeout to 0. This is because dynamic updates are not immediately reflected
            # And this may result in bad UX, and false positive bug reports
            cache.set(cache_key, integration, timeout=0)
        except Integration.DoesNotExist:
            logger.error(f"No integration found for installation {installation_id}", exc_info=True)
            return Response({'message': 'No integration found'}, status=status.HTTP_400_BAD_REQUEST)

    assert integration is not None

    bot_name = integration.github_bot_name
    github_handler = GithubAppHandler(integration)
    # Verify GitHub webhook signature
    try:
        signature_header = request.headers.get('x-hub-signature-256')
        github_handler.verify_signature(body, signature_header)
    except GithubAppHandlerError as e:
        logger.error(f"GitHub webhook signature verification failed: {e}")
        return Response({'message': 'Invalid signature'}, status=status.HTTP_403_FORBIDDEN)

    try:
        # Get event type and validate
        event_type = GitHubEventHandler.find_github_event_type(data)
        if not GitHubEventHandler.is_supported_event(event_type):
            return Response({'message': 'Webhook received'}, status=status.HTTP_200_OK)

        # Get the appropriate event handler
        event_handler = GitHubEventFactory.get_handler(event_type, integration, github_handler)
        
        # Extract event data
        event_data = event_handler.extract_event_data(data)
        
        # Find the appropriate channel
        channel = None
        for channel_itr in integration.channels:
            if channel_itr['name'] == event_data['repository_name']:
                channel = channel_itr
                break

        if not channel:
            logger.error(f"No channel found for repository {event_data['repository_name']}", exc_info=True)
            return Response({'message': 'No channel found'}, status=status.HTTP_400_BAD_REQUEST)

        # Check if we should answer
        if not github_handler.will_answer(event_data['body'], bot_name, event_type, channel['mode']):
            return Response({'message': 'Webhook received'}, status=status.HTTP_200_OK)

        # Create a new binge
        binge = create_fresh_binge(integration.guru_type, None)
        guru_type = integration.guru_type.slug
        
        # Create request using APIRequestFactory
        factory = APIRequestFactory()
        
        # Prepare payload for the API
        payload = {
            'question': github_handler.cleanup_user_question(event_data['body'], bot_name),
            'stream': False,
            'short_answer': True,
            'fetch_existing': False,
            'session_id': binge.id
        }

        if event_data['api_url']:
            payload['github_api_url'] = event_data['api_url']
        
        # Create request with API key from integration
        request = factory.post(
            f'/api/v1/{guru_type}/answer/',
            payload,
            HTTP_X_API_KEY=integration.api_key.key,
            format='json'
        )
        
        # Call api_answer directly
        response = api_answer(request, guru_type)
        
        # Handle the response using the event handler
        event_handler.handle_response(response, event_data, bot_name)

    except Exception as e:
        logger.error(f"Error processing GitHub webhook: {e}", exc_info=True)
        return Response({'message': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
    return Response({'message': 'Webhook received'}, status=status.HTTP_200_OK)

@api_view(['POST'])
@jwt_auth
def list_jira_issues(request, integration_id):
    """
    List Jira issues for a specific integration using a JQL query.
    Requires integration_id in the path and accepts 'jql' as a query parameter.
    """
    try:
        integration = Integration.objects.get(id=integration_id)
    except Integration.DoesNotExist:
        return Response({'msg': 'Integration not found'}, status=status.HTTP_404_NOT_FOUND)

    # Validate user permission (e.g., maintainer or admin)
    try:
        get_guru_type_object_by_maintainer(integration.guru_type.slug, request)
    except PermissionError:
        return Response({'msg': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    except NotFoundError:
        # This shouldn't happen if integration exists, but good practice
        return Response({'msg': 'Associated Guru type not found'}, status=status.HTTP_404_NOT_FOUND)

    # Validate integration type
    if integration.type != Integration.Type.JIRA:
        return Response({'msg': 'This integration is not a Jira integration.'}, status=status.HTTP_400_BAD_REQUEST)

    # Get JQL from query params, default if not provided
    jql_query = request.data.get('jql')
    if not jql_query:
        jql_query = 'ORDER BY created DESC'

    try:
        jira_requester = JiraRequester(integration)
        issues = jira_requester.list_issues(jql_query=jql_query)
        return Response({'issues': issues, 'issue_count': len(issues)}, status=status.HTTP_200_OK)
    except ValueError as e:
        # Handle specific errors from JiraRequester
        error_str = str(e)
        if "Invalid Jira credentials" in error_str:
             return Response({'msg': 'Invalid Jira credentials.'}, status=status.HTTP_401_UNAUTHORIZED)
        elif "Jira API access forbidden" in error_str:
             return Response({'msg': 'Jira API access forbidden. Check user permissions or API key scope.'}, status=status.HTTP_403_FORBIDDEN)
        else:
             logger.error(f"Error listing Jira issues for integration {integration_id}: {e}", exc_info=True)
             return Response({'msg': f'Failed to list Jira issues: {error_str}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except Exception as e:
        logger.error(f"Unexpected error listing Jira issues for integration {integration_id}: {e}", exc_info=True)
        return Response({'msg': 'An unexpected error occurred.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['GET'])
@jwt_auth
def list_zendesk_tickets(request, integration_id):
    """
    List Zendesk tickets for a specific integration.
    Requires integration_id in the path.
    """
    try:
        integration = Integration.objects.get(id=integration_id)
    except Integration.DoesNotExist:
        return Response({'msg': 'Integration not found'}, status=status.HTTP_404_NOT_FOUND)

    # Validate user permission (e.g., maintainer or admin)
    try:
        get_guru_type_object_by_maintainer(integration.guru_type.slug, request)
    except PermissionError:
        return Response({'msg': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    except NotFoundError:
        return Response({'msg': 'Associated Guru type not found'}, status=status.HTTP_404_NOT_FOUND)

    # Validate integration type
    if integration.type != Integration.Type.ZENDESK:
        return Response({'msg': 'This integration is not a Zendesk integration.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        zendesk_requester = ZendeskRequester(integration)
        tickets = zendesk_requester.list_tickets()
        return Response({'tickets': tickets, 'ticket_count': len(tickets)}, status=status.HTTP_200_OK)
    except ValueError as e:
        # Handle specific errors from ZendeskRequester
        error_str = str(e)
        if "Zendesk credentials" in error_str:
             return Response({'msg': 'Missing or invalid Zendesk credentials.'}, status=status.HTTP_401_UNAUTHORIZED)
        elif "Authentication failed" in error_str:
             return Response({'msg': 'Zendesk authentication failed. Check email and API token.'}, status=status.HTTP_401_UNAUTHORIZED)
        elif "Permission denied" in error_str:
             return Response({'msg': 'Zendesk permission denied. Check API token scope.'}, status=status.HTTP_403_FORBIDDEN)
        elif "Resource not found" in error_str or "invalid Zendesk domain" in error_str:
            return Response({'msg': 'Zendesk resource not found or invalid domain.'}, status=status.HTTP_404_NOT_FOUND)
        elif "rate limit exceeded" in error_str:
            return Response({'msg': 'Zendesk API rate limit exceeded.'}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        else:
             logger.error(f"Error listing Zendesk tickets for integration {integration_id}: {e}", exc_info=True)
             return Response({'msg': f'Failed to list Zendesk tickets: {error_str}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except Exception as e:
        logger.error(f"Unexpected error listing Zendesk tickets for integration {integration_id}: {e}", exc_info=True)
        return Response({'msg': 'An unexpected error occurred.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['GET'])
@jwt_auth
def list_zendesk_articles(request, integration_id):
    """
    List Zendesk help center articles for a specific integration.
    Requires integration_id in the path.
    """
    try:
        integration = Integration.objects.get(id=integration_id)
    except Integration.DoesNotExist:
        return Response({'msg': 'Integration not found'}, status=status.HTTP_404_NOT_FOUND)

    # Validate user permission (e.g., maintainer or admin)
    try:
        get_guru_type_object_by_maintainer(integration.guru_type.slug, request)
    except PermissionError:
        return Response({'msg': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    except NotFoundError:
        return Response({'msg': 'Associated Guru type not found'}, status=status.HTTP_404_NOT_FOUND)

    # Validate integration type
    if integration.type != Integration.Type.ZENDESK:
        return Response({'msg': 'This integration is not a Zendesk integration.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        zendesk_requester = ZendeskRequester(integration)
        articles = zendesk_requester.list_articles()
        return Response({'articles': articles, 'article_count': len(articles)}, status=status.HTTP_200_OK)
    except ValueError as e:
        # Handle specific errors from ZendeskRequester
        error_str = str(e)
        if "Zendesk credentials" in error_str:
             return Response({'msg': 'Missing or invalid Zendesk credentials.'}, status=status.HTTP_401_UNAUTHORIZED)
        elif "Authentication failed" in error_str:
             return Response({'msg': 'Zendesk authentication failed. Check email and API token.'}, status=status.HTTP_401_UNAUTHORIZED)
        elif "Permission denied" in error_str:
             return Response({'msg': 'Zendesk permission denied. Check API token scope.'}, status=status.HTTP_403_FORBIDDEN)
        elif "Resource not found" in error_str or "invalid Zendesk domain" in error_str:
            return Response({'msg': 'Zendesk resource not found or invalid domain.'}, status=status.HTTP_404_NOT_FOUND)
        elif "rate limit exceeded" in error_str:
            return Response({'msg': 'Zendesk API rate limit exceeded.'}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        else:
             logger.error(f"Error listing Zendesk articles for integration {integration_id}: {e}", exc_info=True)
             return Response({'msg': f'Failed to list Zendesk articles: {error_str}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except Exception as e:
        logger.error(f"Unexpected error listing Zendesk articles for integration {integration_id}: {e}", exc_info=True)
        return Response({'msg': 'An unexpected error occurred.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@jwt_auth
def validate_ollama_url(request):
    """
    Validate if an Ollama URL is accessible and return available models
    """
    url = request.data.get('url')
    if not url:
        return Response({'error': 'URL is required'}, status=400)
    
    requester = OllamaRequester(url)
    is_healthy, models, error = requester.check_ollama_health()
    
    if not is_healthy:
        return Response({
            'is_valid': False,
            'error': error
        }, status=400)
    
    return Response({
        'is_valid': True,
        'models': models
    })

@api_view(['POST'])
@jwt_auth
def list_confluence_pages(request, integration_id):
    """
    List Confluence pages with optional filtering.
    
    This endpoint handles both spaces and pages, functioning as a combined endpoint:
    - If cql parameter is provided, returns pages matching the query
    - If no parameters are provided, returns all pages
    
    Requires integration_id in the path.
    Optional POST body parameters:
    - cql: Confluence Query Language for filtering pages
    """
    try:
        integration = Integration.objects.get(id=integration_id)
    except Integration.DoesNotExist:
        return Response({'msg': 'Integration not found'}, status=status.HTTP_404_NOT_FOUND)

    # Validate user permission (e.g., maintainer or admin)
    try:
        get_guru_type_object_by_maintainer(integration.guru_type.slug, request)
    except PermissionError:
        return Response({'msg': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    except NotFoundError:
        # This shouldn't happen if integration exists, but good practice
        return Response({'msg': 'Associated Guru type not found'}, status=status.HTTP_404_NOT_FOUND)

    # Validate integration type
    if integration.type != Integration.Type.CONFLUENCE:
        return Response({'msg': 'This integration is not a Confluence integration.'}, status=status.HTTP_400_BAD_REQUEST)

    # Extract parameters from request data with defaults
    cql = request.data.get('query')

    try:
        confluence_requester = ConfluenceRequester(integration)
        result = confluence_requester.list_pages(
            cql=cql 
        )
        return Response(result, status=status.HTTP_200_OK)
    except ValueError as e:
        # Handle specific errors from ConfluenceRequester
        error_str = str(e)
        if "Invalid Confluence credentials" in error_str:
             return Response({'msg': 'Invalid Confluence credentials.'}, status=status.HTTP_401_UNAUTHORIZED)
        elif "Confluence API access forbidden" in error_str:
             return Response({'msg': 'Confluence API access forbidden. Check user permissions or API token scope.'}, status=status.HTTP_403_FORBIDDEN)
        else:
             logger.error(f"Error listing Confluence content for integration {integration_id}: {e}", exc_info=True)
             return Response({'msg': f'Failed to list Confluence pages: {error_str}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except Exception as e:
        logger.error(f"Unexpected error listing Confluence content for integration {integration_id}: {e}", exc_info=True)
        return Response({'msg': 'An unexpected error occurred.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
